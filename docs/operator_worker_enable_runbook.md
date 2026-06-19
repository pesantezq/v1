# Operator Worker — Enable Runbook (Phase 4)

Status: **the worker ships INERT.** This runbook is the explicit, human-driven
sequence to enable autonomous execution once you choose to. As of 2026-06-19 the
CODE for all hardening phases is shipped + live on `main`:

- **Phase 1** (OS isolation / rootless container) — code shipped; system account
  `stockbot-worker` + `/etc/subuid`/`subgid` + linger already provisioned; podman 4.9.3 present.
- **Phase 2** (enforced cost cap) — shipped (`operator_control.cost_cap`).
- **Phase 3** (explicit rollback + quarantine-review + `cancel` verb) — shipped.

What is NOT done (deliberately — these are the human/operator gates):
- The worker's Claude credential dir (interactive OAuth) — **missing**.
- The container image — **not built / digest is the `sha256:0000…` placeholder**.
- `worker_container.enabled` / `autonomous_worker.enabled` — **both `false`**.
- Readiness is **2/5** (`auth`/`bounded_cmd`/`rollback` amber).

> **Governance:** AI can never approve production readiness (CLAUDE.md). Enabling the
> autonomous mutator is an explicit human step. Each `enabled=true` flip below must be
> a deliberate operator action, reversible via the kill-switch.

---

## Pre-flight (verify the already-done provisioning)

```bash
bash scripts/worker_container_setup.sh check
# expect: podman present; stockbot-worker EXISTS; /etc/subuid + subgid found; linger ENABLED
```

## Step 1 — Establish the worker's Claude credentials (INTERACTIVE — operator only)

The worker authenticates via a `~/.claude` login mounted read-only into the container
(NOT an API key — a stray `ANTHROPIC_API_KEY` makes headless claude 401). This is an
interactive OAuth, so it cannot be done headlessly.

```bash
sudo mkdir -p /home/stockbot-worker/.claude-worker
sudo chown stockbot-worker:stockbot-worker /home/stockbot-worker/.claude-worker
sudo chmod 0700 /home/stockbot-worker/.claude-worker
# Interactive: complete the OAuth as the worker user
sudo -u stockbot-worker XDG_CONFIG_HOME=/home/stockbot-worker/.claude-worker claude
#   -> follow the login prompt; then exit. Confirm:
ls -la /home/stockbot-worker/.claude-worker/.claude
```

## Step 2 — Build the container image (as the worker user, rootless)

The image must live in `stockbot-worker`'s rootless storage (the worker runs via
`runuser -u stockbot-worker`). Build AS that user:

```bash
runuser -u stockbot-worker -- bash -lc 'cd /opt/stockbot && bash scripts/worker_container_setup.sh build'
runuser -u stockbot-worker -- podman images localhost/stockbot-worker   # confirm present
```

## Step 3 — Capture + pin the image digest

```bash
runuser -u stockbot-worker -- bash -lc 'cd /opt/stockbot && bash scripts/worker_container_setup.sh digest'
# copy the printed sha256:... and epoch into config.json operator_control.worker_container:
#   "image_digest": "sha256:<captured>",
#   "image_build_ts": <captured epoch>,
# (use the `pin` subcommand to print the full fragment)
bash scripts/worker_container_setup.sh pin
```

Leave `worker_container.enabled` = **false** for now.

## Step 4 — Smoke attestation

```bash
runuser -u stockbot-worker -- bash -lc 'cd /opt/stockbot && bash scripts/worker_container_setup.sh attest'
# writes outputs/operator_control/worker_attestation.json; confirm it verifies
```

## Step 5 — Flip the container on + confirm gates

```bash
# In config.json: operator_control.worker_container.enabled = true
.venv/bin/python -c "from portfolio_automation.operator_worker_readiness import operator_worker_readiness as r; import json; print(json.dumps(r('.')['gates'], indent=2))"
# expect: auth -> green, bounded_cmd -> green (container capability probe passes)
```

## Step 6 — Update the rollback readiness attestation (OPERATOR declares)

Phase 3 added the explicit rollback path (`cancel` + `quarantine-review`/`-discard`/
`-salvage`). Once you've verified it, the operator (not AI) may flip the declared
attestation in `config.json` → `operator_control.readiness_declared.rollback`:

```jsonc
"rollback": { "status": "green", "declared_by": "operator", "declared_at": "<ISO>",
  "evidence": ["operator_control/worker_runner.py", "tests/test_operator_worker_quarantine.py"],
  "note": "explicit rollback: cancel + quarantine discard/salvage (Phase 3, shipped 2026-06-19)" }
```

Re-check readiness → expect **5/5**.

## Step 7 — Enable autonomous execution (the governance gate)

Only after 5/5 + you are satisfied with a kill-switch drill:

```bash
# In config.json: operator_control.autonomous_worker.enabled = true
# Restart the dashboard service so it reflects the new mode:
sudo systemctl restart stockbot-dashboard.service
.venv/bin/python -m operator_control.worker_runner status   # autonomous_enabled: true
```

## Kill-switch (always available)

Any of these instantly disables autonomous execution:
- `touch config/operator_worker.DISABLED`
- `export STOCKBOT_AUTO_APPLY_DISABLED=1` (auto_apply) / set `autonomous_worker.enabled=false`
- The cost cap (`operator_control.cost_cap`) bounds spend even when enabled:
  daily gate defers at `usd_per_day` ($10), per-run rails (`--max-turns` 40,
  `max_run_seconds` 1200) kill the child, post-run flag audits any overage.

## Post-enable health

The daily check (`/daily-tool-analysis`, 6g operator-control line) reports: worker mode,
readiness `n/5`, cost `today_usd/cap_usd (cap_pct%)` (AMBER ≥80% / over-cap event),
`quarantine_pending` (AMBER ≥1). Containment if anything goes wrong: the worker never
merges/pushes; a protected-path or production-impact diff quarantines the worktree;
`quarantine-discard` is the explicit rollback. See `docs/operator_worker_hardening_spec.md`.
