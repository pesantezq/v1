#!/usr/bin/env bash
# Dashboard self-update — Phase C of auto-update-with-manual-intervention.
#
# Fast-forward ONLY, then restart the dashboard service. Triggered EITHER by the
# gated GUI "Apply update" button (spawned detached) OR run manually by the
# operator. NEVER runs on a schedule / unattended.
#
# Safety:
#   * `git merge --ff-only` refuses any non-fast-forward (no divergent-history
#     surprises, no merge/rebase). If the working tree is dirty or not on main,
#     `set -e` aborts BEFORE the restart — a half-update never ships.
#   * The dashboard service runs as root, so a detached child can restart it
#     directly; being detached (start_new_session) it survives the stop.
#   * Kill-switch: config/dashboard_update.DISABLED aborts immediately.
#
# Manual run:  sudo bash scripts/dashboard_update.sh
set -euo pipefail
cd "$(dirname "$0")/.."

if [[ -f config/dashboard_update.DISABLED ]]; then
  echo "dashboard_update DISABLED (kill-switch present) — aborting." >&2
  exit 3
fi

echo "[dashboard_update] fetch origin main"
git fetch origin main

echo "[dashboard_update] checkout main + fast-forward only"
git checkout main
git merge --ff-only origin/main

echo "[dashboard_update] restart stockbot-dashboard.service"
systemctl restart stockbot-dashboard.service
echo "[dashboard_update] done."
