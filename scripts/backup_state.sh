#!/usr/bin/env bash
# Create a local StockBot backup snapshot.
#
# Thin wrapper. All logic lives in portfolio_automation/backup/ so it can be
# tested deterministically; this file exists because the documented entry point
# and the production cron line both name it.
#
# See docs/BACKUP_RECOVERY.md.
set -euo pipefail
umask 077

REPO_ROOT="${REPO_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
PYTHON="${PYTHON:-$REPO_ROOT/.venv/bin/python}"
cd "$REPO_ROOT"

BACKUP_ROOT="${STOCKBOT_BACKUP_ROOT:-/var/backups/stockbot}"
CODE_SHA="$(git rev-parse HEAD 2>/dev/null || echo UNAVAILABLE)"

echo "==> StockBot local backup snapshot"
exec "$PYTHON" -m portfolio_automation.backup.run_backup_state \
    --repo-root "$REPO_ROOT" \
    --backup-root "$BACKUP_ROOT" \
    --code-sha "$CODE_SHA" \
    --retain "${STOCKBOT_BACKUP_RETAIN:-14}"
