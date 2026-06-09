#!/usr/bin/env bash
# Operator-control worker drain (Phase 3).
#
# DEFAULT-INERT: this does NOTHING unless the autonomous worker is enabled
#   (config.json operator_control.autonomous_worker.enabled=true
#    AND STOCKBOT_OPERATOR_WORKER_AUTONOMOUS=1
#    AND no config/operator_worker.DISABLED kill-switch).
# It NEVER merges or pushes; each order runs in an isolated git worktree and is
# left for human review. See docs/operator_control_worker_runner.md.
#
# Manual run:   bash scripts/operator_worker_drain.sh [max_orders]
# Cron (NOT installed by default — operator action; add only after enabling the
# autonomous worker):
#   # 0 * * * *  cd /opt/stockbot && STOCKBOT_OPERATOR_WORKER_AUTONOMOUS=1 \
#   #   bash scripts/operator_worker_drain.sh 10 >> logs/operator_worker_drain.log 2>&1
set -euo pipefail
cd "$(dirname "$0")/.."
exec .venv/bin/python -m operator_control.worker_runner drain --max "${1:-10}" --actor cron
