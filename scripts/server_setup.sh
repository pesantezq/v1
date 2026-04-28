#!/usr/bin/env bash
# =============================================================================
# server_setup.sh — Idempotent Ubuntu VPS setup for StockBot
# Run once after cloning; safe to re-run at any time.
# =============================================================================
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_DIR="$REPO_DIR/.venv"

echo "==> Repo root: $REPO_DIR"

# --- System packages ---
echo "==> Installing system packages..."
apt-get update -qq
apt-get install -y -qq python3 python3-pip python3-venv git ufw curl

# --- Python virtual environment ---
if [ ! -d "$VENV_DIR" ]; then
    echo "==> Creating virtual environment at $VENV_DIR ..."
    python3 -m venv "$VENV_DIR"
else
    echo "==> Virtual environment already exists, skipping creation."
fi

# --- Python dependencies ---
echo "==> Installing Python dependencies..."
"$VENV_DIR/bin/pip" install --quiet --upgrade pip
"$VENV_DIR/bin/pip" install --quiet -r "$REPO_DIR/requirements.txt"

# --- Required directories ---
echo "==> Creating required directories..."
mkdir -p \
    "$REPO_DIR/logs" \
    "$REPO_DIR/data" \
    "$REPO_DIR/outputs/latest" \
    "$REPO_DIR/outputs/performance" \
    "$REPO_DIR/outputs/policy"

# --- Environment file ---
if [ ! -f "$REPO_DIR/.env" ]; then
    echo "==> Copying .env.example to .env (fill in your API keys before running)..."
    cp "$REPO_DIR/.env.example" "$REPO_DIR/.env"
else
    echo "==> .env already exists, skipping copy."
fi

# --- Script permissions ---
chmod +x "$REPO_DIR/scripts/"*.sh

echo ""
echo "============================================================"
echo "  Setup complete. Next steps:"
echo ""
echo "  1. Edit .env with your API keys and email credentials:"
echo "       nano $REPO_DIR/.env"
echo ""
echo "  2. Validate the pipeline:"
echo "       $REPO_DIR/scripts/server_run_pipeline.sh"
echo "         -- or --"
echo "       cd $REPO_DIR && .venv/bin/python run_daily_pipeline.py --debug"
echo ""
echo "  3. Start the Streamlit GUI:"
echo "       $REPO_DIR/scripts/server_start_streamlit.sh"
echo ""
echo "  4. Install the systemd service (optional):"
echo "       cp $REPO_DIR/deploy/stockbot-streamlit.service /etc/systemd/system/"
echo "       systemctl daemon-reload"
echo "       systemctl enable --now stockbot-streamlit"
echo ""
echo "  5. Install the daily pipeline cron:"
echo "       $REPO_DIR/scripts/install_cron.sh"
echo "============================================================"
