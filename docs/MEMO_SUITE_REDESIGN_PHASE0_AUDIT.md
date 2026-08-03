# Memo/Email Suite Redesign — Phase 0 Audit

Snapshot 2026-08-03. Read-only audit. No code changed in this phase.

Every claim below carries `file:line` and was confirmed by reading code or by
inspecting live artifacts/delivery logs on the production VPS. Where I could not
verify something, it is marked **UNVERIFIED**.

---

## 0. Correction to the mission premise (read this first)

The brief states: *"The system currently sends four relevant outputs."* That is
true for three of them. **The Finance Digest is never sent.**

| Email | Sender | Live? | Evidence |
|---|---|---|---|
| Daily Investment Memo | `portfolio_automation/memo_email_sender.py` | **LIVE** | `outputs/policy/memo_delivery_log.jsonl` — 89 `sent:True` entries, one per day through 2026-08-03 |
| Watchlist Digest | `portfolio_automation/watchlist_email_sender.py` | **LIVE** | `outputs/policy/watchlist_email_log.jsonl` — `sent:True` for 08-01, 08-02 |
| Governance Digest | `portfolio_automation/sim_governance/governance_digest.py` | **LIVE** | `outputs/policy/governance_digest_log.jsonl` — `status:sent` 08-02, 08-03 |
| Finance Digest | `email_digest.py` (`FinanceEmailDigest`) | **NEVER SENT** | No caller, no cron entry, no delivery artifact has ever existed |

`FinanceEmailDigest.send_digest` (`email_digest.py:880`) has **zero callers**
outside its own module and tests. `grep` across the repo, `scripts/`, `*.sh`, and
`crontab -l` finds nothing. Only two helpers are consumed at all —
`build_top_summary` and `format_recommendations_for_csv`, both imported by
`finance_analyzer.py:360,382`. There is no `finance_digest_log.jsonl`.

`digest_builder.py` (718 lines) is imported only by `email_digest.py` itself
(lines 35, 125, 284, 590, 698), so it is dead by transitivity.

**Consequence for Phase 3.** The brief asks us to "resolve the current
priority-class problem where a finance recommendation such as an emergency-fund
deficit can be `ACTION_REQUIRED` while Top 3 Actions leads with portfolio
contribution deployment." That contradiction exists in *code* but has never
reached a human. Phase 3 is therefore **activation of a new email**, not repair
of an existing one — a materially different task with a different risk profile
(new outbound delivery path, new recipient config, new cron entry, new
delivery-status artifact, new health coverage). It needs an explicit decision,
not an assumption. See §6.

Also note: `email_digest.py` has **no test file**. The other three senders all do
(`test_memo_email_sender.py`, `test_watchlist_email_sender.py`,
`test_governance_digest.py` + `test_governance_digest_wiring.py`).

---

## 1. Dependency graph

Mapped as `source artifact -> read model -> renderer -> sender -> delivery artifact`.

### 1a. Daily Investment Memo — LIVE

```
outputs/latest/system_decision_summary.json      (daily_memo.py:38)
outputs/latest/decision_plan.json                (daily_memo.py:39)   [SOURCE OF TRUTH]
outputs/latest/memo_coherence.json               <- memo_coherence.run_memo_coherence
outputs/latest/risk_delta.json                   (_RISK_DELTA_REL)
outputs/portfolio/portfolio_snapshot.json        (daily_memo.py:1843)  [ALLOCATION, not holdings]
outputs/sandbox/discovery/*.json                 (daily_memo.py:44-47, 1610-1612)
outputs/latest/simulation_charts.json            (daily_memo.py:2286)
        |
        v  read models
memo_coherence.py:352   compute_funding()          -> funded / deferred / blocking_reason
memo_coherence.py:206   derive_presentation_state() -> presentation state (partial contract EXISTS)
capital_plan_view.py:362 build_capital_plan_view()  -> normalized capital view + money states
memo_datasets.py:154    build_memo_datasets()       -> 5 domain-keyed datasets (observe-only)
        |
        v  renderer
watchlist_scanner/daily_memo.py  (3583 lines)
  _build_verdict()      :800   <-- headline
  capital_plan_view.render_* (text + markdown)
        |
        v  sender
portfolio_automation/memo_email_sender.py
  build_memo_email_message() :353 ; render_memo_html() :287
  entry: run_memo_email_delivery  <- main.py:2938 (non-dry-run) and daily_memo.py:3571
        |
        v  delivery artifact
outputs/policy/memo_delivery_log.jsonl  (idempotency via _already_sent() :511)
```

### 1b. Watchlist Digest — LIVE, operationally isolated

```
outputs/latest/top100_daily.json  (universe + ranking_diagnostics)
outputs/sandbox/discovery/watch_candidates.json (candidates_meta.run_date)
operator alerts artifact
        |
        v  read model
watchlist_email_sender.resolve_watchlist_date() :265   <-- CANONICAL as-of pattern
universe_sanitation.py:486-546  ranking diagnostics (already computed, see §4)
        |
        v  renderer  (text :322-340  |  html :425-436)
        v  sender    build_subject() :523 ; separate SMTP config + recipients
        v  outputs/policy/watchlist_email_log.jsonl
```

Isolation is deliberate and must be preserved: distinct env var namespace
(`WATCHLIST_EMAIL_*`), distinct recipients, distinct log, distinct cron.

### 1c. Governance Digest — LIVE

```
outputs/policy/auto_approval_events.jsonl   <- AA.load_events()
auto_approval.build_summary()               <- circuit_breaker, counters
        |
        X  outputs/promotion_review/operator_approval_packet.json   NOT READ (see §5)
        |
        v  build_governance_digest() :45   (pure)
        v  _render_text() :106  |  _render_html() :136
        v  send_governance_digest() :244  -> outputs/policy/governance_digest_log.jsonl
```

### 1d. Finance Digest — BUILT, NEVER SENT

```
finance_analyzer.py -> FinanceRecommendation[] -> ActionLevel categories
        v  email_digest.build_email_subject() :46 / build_text_body() :118 / build_html_body() :277
        v  FinanceEmailDigest.send_digest() :880   <-- NO CALLER
        v  (no delivery artifact)
```

---

## 2. Reusable infrastructure — do NOT rebuild

The brief warns against duplicate artifacts. These already exist and are
authoritative:

| Need (brief) | Existing implementation | Status |
|---|---|---|
| `run_id`, `source_commit`, `config_hash` | `run_manifest.py` — `build_manifest()` :127, `compute_config_hash()` :49, `source_commit()` :63 | Complete |
| `snapshot_hash`, `market_data_as_of` | `daily_input_snapshot.py` — `build_input_snapshot()` :157; rejects future-dated inputs | Complete |
| Mixed-run / stale detection | `run_manifest.coherent_run_ids()` :246 | **Wired** via `daily_run_status.check_run_coherence` (Phase D1 `3287b37d`). An adversarial probe (`tests/probes/test_probe_freshness_and_cron.py:121`) fails on purpose if it becomes unwired. *(My earlier note that this was unwired was stale — F8.1 is closed.)* |
| Canonical as-of resolution | `watchlist_email_sender.resolve_watchlist_date()` :265 — artifact `generated_at` → `run_date` → wall-clock | Complete; generalize, don't reinvent |
| Funding states | `memo_coherence.compute_funding()` :352 + `capital_plan_view` money states (`_STATE_CONFIRMED`, `not_calculated`) | Reuse; extend vocabulary only |
| Presentation state | `memo_coherence.derive_presentation_state()` :206 | Partial — extend for the 5-axis contract |
| Both-tier governance view | `sim_governance/approval_packet.py` — `build_operator_packet()` :79 (tier-a sim awaiting veto + tier-b production pending), `assess_packet_health()` :143 with `stale_pending_days` | **Complete and unused by the digest** |
| Ranking diagnostics | `universe_sanitation.py:486-546` — `largest_tie_fraction`, `zero_variance`, `zero_information_terms`, `degenerate_ranking` at the ≥0.50 threshold | Complete; only the boolean is consumed |
| Outcome / quant metrics | `quant_feedback.build_quant_feedback()` :127, `attribute_outcomes()` :45; `decision_outcomes.jsonl` | Reuse; do not replace |
| Domain read models | `memo_datasets.py` — 5 domains, `observe_only`, no recompute | Extend rather than add a 6th artifact |

**Net:** Problems 1, 2, 5 and 8 are overwhelmingly *wiring and presentation*
problems over infrastructure that already exists. Very little new calculation is
justified.

---

## 3. Problem 1 — as-of / provenance: CONFIRMED

`memo_email_sender.py:585`

```python
memo_date = now.strftime("%Y-%m-%d")
```

Pure wall-clock. It flows into:
- the subject — `f"Portfolio Daily Memo — {memo_date}"` (:363)
- the attachment filename — `daily_memo_{memo_date}.md` (:384)
- the idempotency key — `_already_sent(run_id, memo_date, ...)` (:511)
- the delivery-log record (:600)

So a memo artifact generated Aug 2 and delivered Aug 3 is labelled Aug 3
everywhere, exactly the bug class the brief describes. The memo renderer never
passes an artifact date to the sender.

Today there is no visible mismatch (artifact written 10:32, delivered same day),
so this is latent — it manifests on any midnight-straddling delivery, retry, or
stale-artifact send. The idempotency coupling makes it worse than cosmetic: a
re-run after midnight UTC gets a *new* dedup key and can re-send the same memo.
That is precisely the failure `resolve_watchlist_date` was written to prevent
(see its docstring, :269-271).

Finance Digest, for its part, has **no date at all** in the populated-subject
branch (`build_email_subject` :53-63 returns `"Finance Digest: 2 Action Required
• …"`) and `date.today()` only in the empty branch (:61) and the monthly memo
(:983).

Governance digest uses injected `now` (`"subject_date": (now or "")[:10]`, :103),
which is better but still delivery-time, not decision-time.

**Fix shape:** one shared resolver generalizing `resolve_watchlist_date`, sourced
from `run_manifest`/`daily_input_snapshot`, returning the full set
(`decision_as_of`, `market_data_as_of`, `generated_at`, `delivered_at`, `run_id`,
`snapshot_hash`, `source_commit`, `config_hash`). All four senders consume it.

---

## 4. Problems 3 & 5 — headline and tie-awareness: CONFIRMED

### Memo verdict ignores funding entirely

`daily_memo.py:800` — `_build_verdict(summary, decision_rows, capital_counts, root)`.

`mood` escalates from decision **urgency** labels and `risk_delta.overall_status`
only (:846-851). `capital_counts` is a count of decisions *by type*
(`SELL`/`SCALE`/`BUY`, :854-856) — **not funding**. There is no funded/deferred
input to the function at all.

Result: 20 BUY + 4 SCALE at `$0` funded, with any single high-urgency row, yields
`"**Action required** — 4 SCALE, 20 BUY"` (:969) — verbatim the headline the
brief forbids. `capital_plan_view` already computes the funded/deferred
reconciliation (`_reconcile()` :679) and the verdict simply does not read it.

Today's live memo shows the milder form: `"**Cautious** — 19 advisory action(s)"`
— still a raw recommendation count presented as the day's verdict.

**Fix shape:** pass the capital-plan reconciliation into `_build_verdict` and let
the funding/operator-action axis dominate the *headline* while `mood` is retained
as supporting context. Presentation-only; `mood`, urgency, and every decision
stay untouched.

### Watchlist renders ties as false ordinals

`universe_sanitation.py:508,533,540` already computes `largest_tie_fraction` and
sets `degenerate_ranking` when `>= 0.5`, and even builds a human warning string
naming the tie count (:546). The sender consumes **only the boolean**
(`watchlist_email_sender.py:328` text, :430 HTML) to print a warning, then
renders candidates as a flat ordinal table using `row['rank']` (:340-345).

So the warning says the ranking is degenerate while the table immediately below
it prints `#5 #6 #7 …` over identically-scored names. The tier data needed to fix
this is already in the artifact.

**Fix shape:** tie-aware *presentation* only — machine ordering in the artifact
stays byte-identical for compatibility, per the brief.

---

## 5. Problem 8 — governance digest: THREE CONFIRMED DEFECTS

1. **Pending production approvals are never fetched.**
   `build_governance_digest` accepts `pending_proposals` (:47) and exposes it as
   `pending_human_proposals` (:97) — but `run_evening_digest` (:297-326) **never
   passes it**. It builds the digest from `AA.load_events()` + `AA.build_summary()`
   only. So `pending_human_proposals` is *always* `[]` in production, no matter
   how many production proposals await a human. `approval_packet.py` —
   which already consolidates exactly both tiers — is never consulted.

2. **HTML/plain-text parity violation.** `pending_human_proposals` is rendered in
   `_render_html` (:162) but appears **nowhere** in `_render_text` (:106-129).
   Since the key is always empty today (defect 1), the divergence is masked — fix
   defect 1 alone and the text email silently omits a critical governance fact
   the HTML email shows. This is the brief's Problem 12 rule, inverted.

3. **"Nothing to do" is asserted from an incomplete predicate.** `_render_text`
   :113 emits `"No auto-approval activity in this period."` when
   `auto_applied`/`human_vetoes`/`rollbacks`/`rollback_conflicts` are all empty —
   ignoring pending production reviews entirely. This is the brief's test
   scenario 10 failing today.

Also absent: any `GOVERNANCE — GREEN|AMBER|RED` rollup, though
`assess_packet_health()` :143 exists to supply it.

These three are the highest-value, lowest-risk fixes in the entire mission:
read-only, additive, and they close a genuine oversight gap.

### 5b. FIXED 2026-08-03 (Phase 5 shipped)

Verified against **live production state**, which made the defect concrete rather
than theoretical: at the time of the fix the operator queue held **10 production
promotion proposals pending human approval** with **0** simulation events. The
digest actually delivered that morning (`governance_digest_log.jsonl`,
`status: sent`, 09:10) therefore read:

```
No auto-approval activity in this period.
```

After the fix, the same inputs render:

```
GOVERNANCE — AMBER · 10 production approvals pending · 0 simulation changes · no authority exceptions
Governance Digest — 2026-08-03
(Simulation-lane auto-approval. Production remains human-gated: this digest
 reports state and cannot approve.)

Why: production_pending:10

PRODUCTION
Pending human approvals (production): 10
  Oldest pending: 0 days
  • prop_45fa3bc94cd4 flock_advisory_context_logic CHAT
  • prop_abf8a81a928e flock_advisory_context_logic NASA
  ...
```

Changes, all read-only and additive:

* `run_evening_digest` now sources tier-b from
  `approval_packet.build_operator_packet` + `assess_packet_health`. A packet that
  cannot be read degrades to **AMBER**, never to an implied empty queue.
* `_production_lines()` renders the tier-b block, called by **both** renderers —
  closing the parity violation at its root rather than patching one format.
* `_assess_status()` computes the GREEN/AMBER/RED rollup. RED on failed
  application, authority-gate breach, circuit breaker, or `packet_gate_drift`
  from packet health; AMBER on awaiting-veto, rollback, rollback conflict,
  pending production review, or stale pending. Status leads both bodies as
  **text**, never colour alone.
* `oldest_pending_age_days` returns `None`, never `0`, on a missing `created_at`.
* `build_subject()` replaces `Governance Digest — <date>` with
  `Governance — 10 Production Reviews · Health AMBER · as of 2026-08-03`.
* The empty steady state still renders one concise GREEN line, and the
  pre-existing `No auto-approval activity` wording is retained for the genuinely
  quiet case (its existing test still passes).

Authority invariants asserted by test: the digest exposes no approval or mutation
affordance, does not mutate its inputs, and always states that production remains
human-gated. A wiring guard test fails **on purpose** if `pending_proposals` stops
being passed — the parameter defaults to `None`, so that regression is otherwise
invisible, which is exactly how it went unnoticed for months.

Tests: `tests/test_governance_digest_two_tier.py` (27), plus all 15 pre-existing
governance-digest tests still green.

---

## 6. Decisions required before coding

1. **Finance Digest scope.** It is unsent dead code. Options: (a) activate it as a
   real fourth email (new delivery path + recipients + cron + delivery artifact +
   health coverage); (b) redesign it as a *section* of an existing live email;
   (c) leave it dormant and descope Phase 3; (d) delete it as debt. The brief
   assumes it is live, so this cannot be inferred.

2. ~~**Ring-fenced vs canonical capital hierarchy**~~ — **RESOLVED by config
   inspection: budgets are NOT ring-fenced. Option A applies.**

   `config.json finance_analysis.priorities` is a **single 1-5 scale spanning both
   domains**: `{"savings": 3, "emergency_fund": 4, "portfolio_drift": 3,
   "taxes": 3, "budget": 2}`. There is no per-lane budget anywhere in config — no
   independent cash-safety and investment monthly amounts. Per the brief's own
   rule ("do not infer ring-fencing unless configuration explicitly establishes
   it"), option **A — one canonical capital-priority hierarchy including
   household cash safety** — is the required semantics. And the config already
   ranks `emergency_fund: 4` **above** `portfolio_drift: 3`.

   Why the contradiction still occurs: those priorities *are* loaded
   (`FinanceConfig.priority_emergency` etc., `finance_analyzer.py:180-186`) and
   *do* reach the score via `calc_priority()` (`scoring.py:287`, mapping 1-5 →
   2-10, e.g. priority 4 → 8) — but `priority` is only **one of five components**
   in `ScoringComponents` alongside `severity`, `persistence`, `impact`, and
   `confidence` (`scoring.py:478-486`). `ActionLevel` is then derived from the
   composite 0-100 score alone (`scoring.py:30-35`). So a high-severity
   portfolio-drift item can and does outrank a higher-priority emergency-fund
   deficit — the operator's stated hierarchy is diluted rather than honoured.

   **Constraint on the fix:** the brief forbids changing scoring semantics in this
   task. Therefore the canonical hierarchy must be applied as a **presentation
   ordering** over the existing scored recommendations (cash-safety outranks
   optional deployment), leaving `ScoringComponents`, `calc_priority`, and
   `ActionLevel` untouched. Any re-weighting of `priority` within the composite is
   a separate, separately-reviewed proposal.

3. **Phase ordering / batch size.** The brief prefers small reviewable phases. My
   recommendation, ordered by value-per-risk: Phase 1 (provenance) → Phase 5
   (governance, three real defects) → Phase 2 (memo headline) → Phase 4
   (watchlist ties) → Phase 6 (metric contracts) → Phase 3 (finance, pending
   decision 1) → Phase 7 (Strategy Lab challengers).

---

## 6b. Finance Digest — scoped recommendation (operator decision 2026-08-03: scope + suggest, ship health coverage)

**Not built in this pass.** Activating it would create a new outbound email path,
which is a deliberate operational decision rather than a defect repair. What
follows is the scoped proposal; health coverage for its dormant state **has**
shipped (`portfolio_automation/email_suite_health.py`).

### Recommended shape, if activated

| Item | Decision |
|---|---|
| Delivery gate | Its own env namespace (`FINANCE_DIGEST_*`), mirroring the watchlist sender's deliberate isolation. Do **not** reuse the memo's `MEMO_EMAIL_*` or the digest's legacy `EMAIL_SENDER/RECIPIENT/PASSWORD` — see the two-env-name trap already recorded for this repo. |
| Cadence | Weekly or monthly, **not** daily. Its subject matter (reserve adequacy, contribution policy, allocation policy, trajectory) does not change daily, and a daily send would guarantee it becomes noise the operator filters. |
| As-of semantics | Consume the shared resolver from Phase 1. It currently has *no* date in its populated-subject branch. |
| Priority semantics | **Option A, canonical hierarchy** — settled by config, see §6.2. Cash safety must be able to outrank optional deployment, applied as presentation ordering only. |
| Delivery artifact | `outputs/policy/finance_digest_log.jsonl`, matching the other three senders' idempotency-log pattern. |
| Health coverage | Flip `SUITE["finance_digest"]["live"] = True` in `email_suite_health.py` and give it `amber_after_days` / `red_after_days` for the chosen cadence. The registry entry already exists. |
| Tests | It has **no test file at all** today. Activation must ship one. |
| Projections | Replace the to-the-cent 10-year figures with the brief's three named scenarios and disclosed assumptions before any send. Do not present a scenario range as a confidence interval. |

### Alternative worth weighing first

Because the finance content is *policy*, not *daily market state*, folding it
into the existing live memo as a clearly-labelled weekly block would deliver most
of the operator value with none of the new-delivery-path risk. That trades the
brief's "distinct job" separation for a materially smaller surface. My
recommendation is to decide this on cadence: if the content is genuinely weekly,
a separate weekly email is right; if it is a handful of lines, make it a memo
block.

### Deliberately NOT recommended

Deleting it. `digest_builder.py` + `email_digest.py` carry ~1,800 lines of
finance-reporting logic (scenario projections, reserve maths, contribution
policy) that is expensive to rebuild and is not wrong — merely unwired. It is
dormant debt, now *recorded* debt, not garbage.

---

## 7. Invariant surface to protect

Confirmed read-only/observe-only by construction in the modules we will touch:
`memo_coherence` (module docstring enumerates the prohibitions),
`memo_datasets` (`feeds_decision_engine=false`), `capital_plan_view`
(read-only view model), `approval_packet` ("NEVER mutates governance state").

Nothing in the planned presentation work needs to write `decision_plan.json`,
touch a protected score, alter target allocations, or create a production
mutation path. The watchlist sender's isolation (separate env namespace,
recipients, log, cron) must survive Phase 1's shared-resolver refactor — the
resolver must be a *library*, not a merged sender.
