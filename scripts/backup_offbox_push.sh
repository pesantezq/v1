#!/usr/bin/env bash
# Produce the encrypted off-box artifact.
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

# Local-only by DEFAULT. Pass --upload (or set STOCKBOT_OFFBOX_UPLOAD=1) to
# publish. Nothing here stores a credential; gh supplies its own.
BACKUP_ROOT="${STOCKBOT_BACKUP_ROOT:-/var/backups/stockbot}"
KEY_FILE="${STOCKBOT_BACKUP_KEY_FILE:-/root/.stockbot_backup_key}"
GH_REPO="${STOCKBOT_BACKUP_GH_REPO:-pesantezq/stockbot-backups}"
RETENTION="${STOCKBOT_OFFBOX_RETENTION:-14}"

EXTRA=()
if [ "${STOCKBOT_OFFBOX_UPLOAD:-0}" = "1" ]; then EXTRA+=(--upload); fi

echo "==> StockBot encrypted backup artifact"
exec "$PYTHON" -m portfolio_automation.backup.run_offbox_push \
    --backup-root "$BACKUP_ROOT" \
    --key-file "$KEY_FILE" \
    --gh-repo "$GH_REPO" \
    --retention "$RETENTION" \
    "${EXTRA[@]}" "$@"
