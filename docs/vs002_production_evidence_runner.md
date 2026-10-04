# VS-002 production historical-price evidence runner

The governed, supported entry point for the bounded VS-002 historical-price
evidence build (mission `northstar_0c_historical_price_evidence_for_vs002`).

```
python -m portfolio_automation.vs002_evidence.production_runner \
    --repo-root /opt/stockbot/current [--json]
```

It orchestrates the existing library layers — `builder.build` →
`consumer.validate` → `readiness.evaluate` — through
`FMPDividendAdjustedProvider`, and closes three behaviours that must not be
inherited from the general FMP client.

## Boundaries

- **Production host owns the FMP credential.** The credential is read on the
  production VPS through the existing secret mechanism only. The research /
  QPC engineering environment never receives it, and the runner never prints
  `FMP_API_KEY` or a credential-bearing URL.
- **Fixed endpoints.** The authoritative risk-return source is only
  `/stable/historical-price-eod/dividend-adjusted`; the benchmark
  dividend-adjustment witness additionally reads
  `/stable/historical-price-eod/full` for SPY only. There is no CLI flag for
  the endpoints, symbols, benchmark, `MIN_COHORTS`, retries, cache, provider, or
  a synthetic mode — the frozen contract is the only configuration. A test-only
  injection seam exists internally.
- **23 actual HTTP attempts maximum, across two authorized endpoint classes.**
  22 dividend-adjusted acquisitions (SPY first, then each frozen non-benchmark
  symbol exactly once) plus exactly ONE benchmark `/stable/historical-price-eod/full`
  companion witness. Order: SPY dividend-adjusted → SPY `/full` companion →
  verify the dividend-adjustment witness → the remaining 21 dividend-adjusted
  symbols, so a semantic mismatch costs at most two calls. Both endpoint classes
  consume ONE shared mission budget/counter; there is no 24th request. The
  companion `/full` response is **advisory-only**: it witnesses the benchmark's
  dividend adjustment (`adjClose_divadj / close_full`, which isolates the
  dividend component since FMP's `/full` close is split-adjusted) and NEVER
  participates in eligibility, returns, beta, cohorts, signal inclusion or
  scoring. Like every artifact in this `experimental_noncanonical` package it
  grants no authority (see the manifest `authority_statement`).
- **No retry.** Exactly one outbound request per symbol; a 429/5xx/timeout/
  transport error fails the symbol (and the build), it is never retried.
- **No cache, no stale fallback.** The strict-live path reads neither fresh
  nor stale cache and never serves stale data; on budget exhaustion or any
  error it fails closed.
- **Immutable, content-addressed package.** The build writes a fresh per-run
  staging directory `outputs/vs002_evidence/.staging-<run-id>/`; only after
  `consumer.validate` passes is it published, by atomic rename, to
  `outputs/vs002_evidence/packages/<package_id>/`. An existing
  `packages/<package_id>` is **never** overwritten (an identical id means
  identical evidence already exists).
- **Validate before publish.** `consumer.validate` is mandatory and
  load-bearing; a `SnapshotInvalid` prevents publication. `readiness.evaluate`
  runs only on the returned `ValidatedSnapshot`.
- **Readiness only.** The runner assesses readiness; it does **not** execute
  VS-002. Phase 0D remains prohibited.

## Results and exit codes

| result | exit | meaning |
|---|---|---|
| `PASS` | 0 | valid package, `VS002_READY`, ≥ `MIN_COHORTS` cohorts |
| `NOT_READY` | 10 | valid package; the **only** readiness blocker is `INSUFFICIENT_NON_OVERLAPPING_COHORTS` |
| `BLOCKED_PREFLIGHT` | 20 | a precondition failed before any network attempt (credential, signal DB/schema, staging namespace, frozen-contract integrity) |
| `FAIL_PROVIDER` | 30 | live acquisition failed (entitlement/transport/shape/semantics) |
| `FAIL_BUILD` | 31 | the build failed for a non-acquisition reason |
| `FAIL_PACKAGE_VALIDATION` | 32 | `consumer.validate` refused the package |
| `FAIL_READINESS_CONTRACT` | 33 | valid package, but a readiness blocker other than the cohort deficit |

A failed or partial acquisition is never reported as `NOT_READY`.

## Agent Export boundary

The generic Agent Export allowlist no longer carries VS-002 evidence. The old
flat `vs002_evidence/{signals,returns,manifest}.json` entries are removed, so a
stale flat file can never be exported as though it were this runner's package.
The runner's package (seven artifacts under `packages/<package_id>/` —
`signals.json`, `returns.json`, `bars.json`, `bars_raw.json`,
`bars_witness_raw.json`, `bars_snapshots.json`, `manifest.json`) is **not**
exported by the generic path; it is carried only by the separate, explicitly
authorized transport mission, which selects **one** `package_id` (never
`latest`, never a wildcard).

## Operator sequencing

The strict sequence, each step separately authorized. Status as of 2026-10-01:

1. **production runner PASS** (this component) — builds, validates, assesses
   readiness on the VPS; publishes the immutable package. **DONE** — frozen
   package `vs002evd_77469725f5592e6df33742b68a31ae1e` on production SHA `7f2d410`.
2. **explicit package transport** — one named `package_id` from production to the
   lab. **DONE** — byte-exact (transport digest `4e1f5a6f…`).
3. **the lab independently validates** the exact `package_id`, artifact digests,
   EvidenceRefs, witness and readiness (`consumer.validate` on the lab side).
   **DONE** — `VS002_LAB_REVALIDATED=YES` (2026-09-28); witness PASS, readiness
   `VS002_READY`.
4. **final result-blind preregistration frozen** — the six operator rulings and
   the exact statistical contract. **DONE** — `evals/vertical_slice/VS-002_preregistration.json`
   (schema `engineering.vertical_slice.preregistration.v1`, freeze digest
   `b7da049b…`), merged via PR #58 @ `556e554a`. Grants no execution authority.
5. **deterministic VS-002 result-runner IMPLEMENTATION** — build and certify the
   runner against the frozen preregistration + frozen package identity using
   synthetic/mutation evidence. This was the `northstar_vs002_result_runner`
   mission. **DONE — COMPLETE and durable on `main`** via PR #60 (merge
   `eaee0584`, post-merge CI green; certified head `c9a088e`):
   `portfolio_automation/vs002_evidence/result_runner.py` + `result_contract.py`,
   certified with synthetic and mutation fixtures only
   (`tests/test_vs002_result_runner*.py`). The real frozen evidence package was
   **NOT opened, enumerated, validated, or evaluated**, and NO real VS-002 result
   was computed. Being durable on `main` grants NO real-experiment-execution authority.
6. **continuous mission orchestration foundation** — the CURRENT authorized bounded
   step (`northstar_continuous_mission_orchestration_foundation`). It automates
   exact-head CI/review gating, merge, post-merge certification, protected state
   transition handoff, and dispatch of only already-authorized Claude missions.
   It grants no real-evidence/execution/production/capital/C1 authority.
7. **thin VS-002 execution-adapter foundation** — already operator-authorized but
   currently **PAUSED** with its exact scope preserved machine-readably. After the
   orchestration foundation is durable, resume the trusted path (explicit package
   path -> `consumer.validate` -> `ValidatedSnapshot` -> `verify_evidence_binding`
   -> `result_runner.run` -> canonical `ExperimentResult` -> governed immutable
   result artifact) with package-integrity wiring, SYNTHETIC fixtures only.
   Execution-time package integrity = recomputable artifact digests + deterministic
   `package_id` (`consumer.validate`) PLUS
   `manifest code_sha == frozen source_production_sha`; the frozen
   `package_transport_digest` is a **HISTORICAL ATTESTATION ONLY** and is not
   recomputed. Does NOT open the real package and does NOT execute the experiment.
8. **one frozen VS-002 execution** — a SEPARATE, later, separately-authorized
   mission (`northstar_vs002_frozen_execution`). NOT authorized.

The authoritative frozen experiment contract is the preregistration JSON in step
4; this document summarizes the sequencing only and does not restate those rules.
This runner produces and assesses a production evidence package; it does not
transport it and does not run the experiment.
