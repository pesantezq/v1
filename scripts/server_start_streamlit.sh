#!/usr/bin/env bash
# =============================================================================
# server_start_streamlit.sh — Launch the StockBot Streamlit GUI
# For manual runs; for persistent hosting use the systemd service instead.
# =============================================================================
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_ACTIVATE="$REPO_DIR/.venv/bin/activate"

cd "$REPO_DIR"

if [ ! -f "$VENV_ACTIVATE" ]; then
    echo "ERROR: Virtual environment not found at $REPO_DIR/.venv" >&2
    echo "       Run scripts/server_setup.sh first." >&2
    exit 1
fi
# shellcheck source=/dev/null
source "$VENV_ACTIVATE"

# Load .env safely
if [ -f "$REPO_DIR/.env" ]; then
    set -o allexport
    # shellcheck source=/dev/null
    source <(grep -E '^[A-Za-z_][A-Za-z0-9_]*=' "$REPO_DIR/.env")
    set +o allexport
fi

echo "==> Starting Streamlit on 0.0.0.0:8501 ..."
exec streamlit run gui/app.py \
    --server.address 0.0.0.0 \
    --server.port 8501
