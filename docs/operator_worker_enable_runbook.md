# Operator Worker — Enable Runbook (Phase 4) — VALIDATED 2026-06-19

The worker ships INERT. This is the exact, **tested** sequence to enable autonomous
execution. As of 2026-06-19 everything up to the two enable flags is done + pushed
(`origin/main`); the steps below are the remaining **operator-only** actions (the
safety classifier correctly reserves the credential login + the `enabled` flips for a
human — an AI cannot self-bootstrap autonomous-execution credentials or flip its own
enable gate).

## Already done (provisioning — live on main)
- ✅ Image built: `localhost/stockbot-worker` (rootless, as `stockbot-worker`).
- ✅ Digest + build_ts pinned in `config.json`
  (`sha256:3cd3b7bde4769942e6c6cbacc8d2d9733f16d36bbb5d04c29ec43b6dd9c5c309`).
- ✅ `credentials_dir` corrected to the worker's real home `/var/lib/stockbot-worker/.claude-worker`.
- ✅ `--userns=keep-id` fix (the worker can read its own 0600 creds; verified).
- ✅ Locked-down smoke passed: claude 2.1.181 + python 3.12.13 run under the full
  isolation profile (keep-id, read-only, no-new-privileges, cap-drop=ALL, pid/mem/cpu
  limits) as uid 1000.
- ⏳ Both enable flags still `false`. Creds dir not yet created (needs your login).

> All worker podman commands run as `stockbot-worker` with this env (the session bus is
> required or rootless `crun` fails with an sd-bus "Interactive authentication" error):
> `runuser -u stockbot-worker -- env HOME=/var/lib/stockbot-worker XDG_RUNTIME_DIR=/run/user/1000 DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus podman ...`

## Step 1 — Worker Claude login (INTERACTIVE — you must run this)

Log the worker into Claude *inside the container* (the image bundles the claude CLI),
with the creds dir mounted writable. `--userns=keep-id` makes the write land as the
host worker user so the file is a proper 0600 owned by `stockbot-worker`:

```bash
sudo install -d -o stockbot-worker -g stockbot-worker -m 700 /var/lib/stockbot-worker/.claude-worker
runuser -u stockbot-worker -- env \
  HOME=/var/lib/stockbot-worker XDG_RUNTIME_DIR=/run/user/1000 \
  DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus \
  podman run -it --rm --userns=keep-id \
    -v /var/lib/stockbot-worker/.claude-worker:/home/worker/.claude:rw \
    localhost/stockbot-worker claude
# complete the OAuth in your browser, then /exit. Confirm:
ls -l /var/lib/stockbot-worker/.claude-worker/.credentials.json   # expect -rw------- stockbot-worker
```

After this, tell Claude Code — it can run a **read-only end-to-end smoke** (a headless
`claude -p` inside the production launch profile, creds mounted `:ro`) to prove the
worker authenticates *before* you flip anything.

## Step 2 — Flip container isolation on (you run this)

```bash
# config.json -> operator_control.worker_container.enabled = true
python3 - <<'PY'
import json; p="config.json"; c=json.load(open(p))
c["operator_control"]["worker_container"]["enabled"]=True
json.dump(c, open(p,"w"), indent=2)   # NOTE: re-indent reflows the file; or edit the one line by hand
print("container enabled")
PY
.venv/bin/python -c "from portfolio_automation.operator_worker_readiness import operator_worker_readiness as r; print(r('.')['gates']['auth'])"
# expect auth -> green (podman + image + pinned digest + rootless all present)
```
> Prefer a **one-line hand edit** of `worker_container.enabled` to avoid the json.dump
> reflow (it rewrites the whole file + escapes em-dashes). Same for the rollback block.

## Step 3 — Declare the rollback gate green (you, the operator)

Phase 3 shipped the explicit rollback (`cancel` + `quarantine-review`/`-discard`/
`-salvage`). Once you've reviewed it, set in `config.json`
`operator_control.readiness_declared.rollback.status` = `"green"` (update `declared_at`,
add the Phase-3 evidence). Re-check: readiness should reach **5/5**.

## Step 4 — Enable autonomous execution (the production gate — you run this)

Only at 5/5 and after a kill-switch drill:

```bash
# config.json -> operator_control.autonomous_worker.enabled = true   (hand-edit the one line)
sudo systemctl restart stockbot-dashboard.service
.venv/bin/python -m operator_control.worker_runner status   # autonomous_enabled: true
```

## Kill-switch (always)
- `touch config/operator_worker.DISABLED` (instant disable)
- set `autonomous_worker.enabled=false`
- Cost cap bounds spend even when enabled: daily gate defers at `usd_per_day` ($10);
  per-run rails (`--max-turns` 40, `max_run_seconds` 1200) kill the child; over-cap audited.

## Containment (if a run misbehaves)
Never merges/pushes; protected-path or production-impact diff quarantines the worktree;
`worker_runner quarantine-discard --id <id>` is the explicit rollback;
`quarantine-salvage --id <id>` prints the manual integration command. Daily check 6g
surfaces `quarantine_pending` (AMBER ≥1) + cost-cap utilization.
