#!/usr/bin/env bash
# Prove a backup restores. Read-only; scratch tree only.
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

# Accepts an encrypted *.tar.gz.enc OR a plaintext snapshot directory.
# Restores into a temporary scratch tree and NEVER writes to the live repo.
KEY_FILE="${STOCKBOT_BACKUP_KEY_FILE:-/root/.stockbot_backup_key}"

if [ "$#" -lt 1 ]; then
    echo "usage: $(basename "$0") <artifact.tar.gz.enc | snapshot-dir>" >&2
    exit 2
fi

echo "==> StockBot restore proof"
exec "$PYTHON" -m portfolio_automation.backup.run_restore_verify \
    --key-file "$KEY_FILE" "$@"
