# Dashboard Auto-Update with Manual Intervention

Status: **shipped** (Phase A+B live; Phase C built, default-inert). The dashboard
**auto-detects** stale served code and surfaces it; the **apply is manual**
(never unattended). Design: `docs/superpowers/specs/2026-06-09-dashboard-auto-update-design.md`.

## Why
The dashboard runs `uvicorn … :8502` with **no `--reload`**, so new code only
goes live after a service restart. Data already auto-refreshes (HTMX 60–120s);
this closes the *code*-staleness gap — the one thing that can't self-fix is what
gets flagged.

## Phase A — detection (automatic, read-only)
- At startup the service stamps the SHA it is serving →
  `outputs/operator_control/.running_sha`.
- `gui_v2/data/deploy_status.py` compares it to `origin/main` (throttled
  read-only `git fetch`, ≤1/90s) → `up_to_date` / `update_available` (fast-forward,
  N behind) / `divergent` / `unknown`. Pure read-only.
- System tab shows a **Deployment card** (always) + an **Update-available banner**
  (when behind). Surfaces automatically via the existing 120s refresh — no
  restart needed to *see* staleness.

## Phase B — manual command (zero privilege, default)
- The banner shows the exact copy-paste command:
  ```bash
  cd /opt/stockbot && git fetch origin && git merge --ff-only origin/main \
    && sudo systemctl restart stockbot-dashboard.service
  ```
- A **"Mark update requested"** button (`POST /dashboard/operator/request-update`)
  records a `deploy_update_requested` audit event and **executes nothing**.

## Phase C — one-click apply (gated, default-inert)
- An **"Apply update & restart"** button (`POST /dashboard/operator/apply-update`)
  spawns a **detached** privileged updater (`scripts/dashboard_update.sh`) that
  `git fetch` → `checkout main` → `merge --ff-only origin/main` → `systemctl
  restart stockbot-dashboard.service`. The web process never restarts itself; the
  detached updater survives the stop.
- **Gate (all required):** `GUI_V2_DEPLOY_APPLY=1` AND no
  `config/dashboard_update.DISABLED` kill-switch. Default OFF → the button is
  hidden and the endpoint returns 403.
- **Refuses** anything that isn't a clean **fast-forward** (`409`); divergent
  histories are manual-only (no merge/rebase surprises). Records
  `deploy_update_applied` (`from_sha → to_sha`).

### Enabling Phase C
1. Set `GUI_V2_DEPLOY_APPLY=1` in the service environment + restart once.
2. The dashboard runs as root, so the detached updater can `systemctl restart`
   directly — no sudoers entry needed in this deployment. (If the service ever
   runs unprivileged, add a scoped sudoers rule for *only* `systemctl restart
   stockbot-dashboard` + `git -C /opt/stockbot fetch`/`merge --ff-only`.)
3. Emergency stop: `touch config/dashboard_update.DISABLED` (forces Mode B only;
   the updater script also self-aborts on it).

### Rollback
```bash
git -C /opt/stockbot reset --hard <from_sha>   # from the audit event
sudo systemctl restart stockbot-dashboard.service
```

## What it does NOT do
- No unattended/cron auto-deploy; detection is automatic, apply is always a
  human action.
- No non-fast-forward updates; no merge/rebase of divergent history.
- No blanket sudo; the updater does only ff-pull + restart.
- Does not touch the data path (already auto-refreshes).
