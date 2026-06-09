# Dashboard Auto-Update with Manual Intervention — design + plan

Date: 2026-06-09 · Status: design, pending approval. Author: Claude (operator-directed).

## Goal

The dashboard should **automatically detect** when it is serving stale code (or
stale data) and **surface it prominently**, while the actual update (code pull +
service restart) happens only on a **deliberate manual trigger** — never
unattended. "Auto-update with manual intervention": detection + display are
automatic; the apply is gated behind a human action.

## Why (problem found this session)

- **Data already auto-refreshes** via HTMX (`hx-trigger every 60–120s`). No change.
- **Code does NOT auto-reload**: `uvicorn gui_v2.app:app … :8502` runs without
  `--reload`, so new routes/templates serve stale until
  `sudo systemctl restart stockbot-dashboard.service`. Today that step is
  invisible — stale code has already been mistaken for a firewall/outage.
- A web process must **not** restart its own systemd unit (it would kill itself
  mid-request) and self-restart is permission-blocked. So a privileged,
  *separate* updater is required, and it must only fire on a manual gate.

## Design — three layers

### 1. Detection (automatic, observe-only, zero-risk)
A `deploy_status` loader computes, on each dashboard render:
- `running_sha` — git SHA captured at process **startup** (a stamp written when
  the service boots, e.g. `outputs/operator_control/.running_sha`), so it
  reflects the *code actually being served*, not the working tree.
- `latest_sha` — `git rev-parse origin/main` after a periodic read-only
  `git fetch` (cheap; cached ~120s).
- `update_available` = `running_sha != latest_sha` AND `latest_sha` is a
  fast-forward descendant of `running_sha` (else `divergent` → manual only).
- `commits_behind`, `artifact staleness` (reuse `daily_run_status` freshness).

Pure read-only. Cannot change anything.

### 2. GUI surface (auto-displays via the existing 120s refresh)
- System-tab **"Deployment" card**: `running 1a58a05 · latest a7797f2 · 3
  commits behind · update available`, or `up to date`.
- A dismissible **"Update available"** banner when `update_available`.
- Because the card reads git live, staleness shows up **without** a restart —
  the one thing that genuinely can't self-fix is what gets surfaced.

### 3. Apply — the manual intervention (gated)
The web process never restarts itself; a **separate privileged updater** does.
Two intervention modes (ship A→B→C):

- **Mode B — manual command (default, zero privilege):** the card/banner shows
  the exact copy-paste block and records an `update_requested` audit event. The
  operator runs it. Safe everywhere.
  ```bash
  cd /opt/stockbot && git fetch origin && git merge --ff-only origin/main \
    && sudo systemctl restart stockbot-dashboard.service
  ```
- **Mode C — one-click apply (opt-in, gated):** an "Apply update" button →
  writes an update-request flag + spawns a **detached privileged updater**
  (`scripts/dashboard_update.sh`, allowed via a narrowly-scoped sudoers rule for
  *only* `systemctl restart stockbot-dashboard` + `git -C /opt/stockbot fetch` +
  `merge --ff-only`). The updater: `git fetch` → `merge --ff-only` (refuse if not
  a fast-forward) → restart the service → optionally trigger `run_daily_safe.sh`
  for data. The **click is the manual intervention**; nothing fires unattended.
  Gated like the autonomous worker (auth + `GUI_V2_DEPLOY_APPLY=1` + kill-switch).

## Tie-in to operator-control (reuse, don't reinvent)
Model as an operator-control probe + skill:
- Probe `deploy.update_available` (source_view: system).
- Skill `apply_dashboard_update` (mode `safe_repair`-like; the apply is the
  privileged updater, not a claude worker).
- Reuse the **audit log** (`from_sha → to_sha, actor, ts`), the **kill-switch**
  pattern, and the **"click is the gate"** dispatch pattern from Phase 4.

## Safety model
- **Fast-forward only** — refuse any update that isn't a clean ff of the running
  code (no merge/rebase surprises, no divergent histories from concurrent work).
- **Never unattended** — detection is automatic; apply requires the click (Mode
  C) or the operator command (Mode B). No cron auto-deploy.
- **Scoped privilege** — the updater's sudoers rule covers only the restart +
  ff-pull, not blanket sudo.
- **Audited + reversible** — every apply records `from_sha`/`to_sha`; rollback =
  `git reset --hard <from_sha> && restart` (documented).
- **Kill-switch** — `config/dashboard_update.DISABLED` forces Mode B only.

## Phased plan (each phase independently shippable)

### Phase A — detection + surface (no privilege, no restart automation)
1. Startup SHA stamp: write `running_sha` when the service boots
   (`gui_v2/app.py` startup hook → `.running_sha`). *(Test: stamp written.)*
2. `gui_v2/data/deploy_status.py`: compute running/latest/behind/update_available
   (read-only `git`; degrade gracefully offline). *(Tests: up-to-date, behind,
   divergent, git-unavailable.)*
3. System-tab **Deployment card** + **banner** (reuse `card()` + `_ui` macros).
   *(Test: card renders all states; no controls.)*
4. Docs + roadmap. Ships pure observe-only.

### Phase B — manual command surface
5. Card/banner shows the exact update command + an `update_requested` audit
   event (a tiny POST that only records intent, spawns nothing). *(Tests:
   command rendered; audit recorded; nothing executed.)*

### Phase C — gated one-click apply (opt-in)
6. `scripts/dashboard_update.sh` (ff-only pull + restart; refuses non-ff).
7. Scoped sudoers entry (documented; operator installs it).
8. Gated "Apply update" button → detached updater; `GUI_V2_DEPLOY_APPLY=1` +
   kill-switch. *(Tests: gate off → button absent/refused; ff-only enforced;
   audit from→to; mocked subprocess.)*
9. Docs: activation runbook + rollback.

## What it does NOT do
- No unattended/cron auto-deploy or self-restart.
- No non-fast-forward updates (no merge/rebase of divergent history).
- No blanket sudo; the updater is scoped to restart + ff-pull only.
- Does not touch the data path (already auto-refreshes); data rerun is optional
  inside the apply, off by default.

## Recommendation
Ship **Phase A + B first** — they fully deliver "auto-detect staleness + a
clear manual apply" with **zero** privilege/restart risk, and immediately close
the silent-stale-code gap. Add **Phase C** only if you want the one-click apply
(it introduces scoped privilege + a self-restart helper).
