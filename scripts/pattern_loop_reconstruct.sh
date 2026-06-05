#!/usr/bin/env bash
# Pattern-Improvement Loop — historical signal reconstruction (sub-project F).
#
# Populates the 5y price archive, reconstructs pattern-family signals point-in-time,
# runs the look-ahead audit, then runs the loop over the reconstructed history so the
# OOS window matures NOW. Observe-only producers; the loop's auto-apply (if enabled)
# refuses reconstructed evidence unless the look-ahead audit is clean (fail-closed).
#
# FMP-only (free) for prices; no AI spend in the reconstruction itself. Best-effort:
# logs and exits non-zero on failure.

set -uo pipefail

export HOME="${HOME:-/root}"
export PATH="/root/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

REPO_ROOT="/opt/stockbot"
cd "${REPO_ROOT}" || { echo "FATAL: cannot cd to ${REPO_ROOT}" >&2; exit 2; }

PYTHON_BIN="${REPO_ROOT}/.venv/bin/python"
LOG_DIR="${REPO_ROOT}/logs"
mkdir -p "${LOG_DIR}"
LOG_FILE="${LOG_DIR}/pattern_loop_reconstruct_$(date -u +%Y-%m).log"

load_dotenv_file() {
  local env_file="$1"
  local line trimmed key value
  [ -f "$env_file" ] || return 0
  while IFS= read -r line || [ -n "$line" ]; do
    line="${line%$'\r'}"
    trimmed="${line#"${line%%[![:space:]]*}"}"
    [ -z "$trimmed" ] && continue
    [ "${trimmed:0:1}" = "#" ] && continue
    trimmed="${trimmed#export }"
    [[ "$trimmed" != *=* ]] && continue
    key="${trimmed%%=*}"
    value="${trimmed#*=}"
    if [[ "$value" =~ ^\".*\"$ ]] || [[ "$value" =~ ^\'.*\'$ ]]; then
      value="${value:1:${#value}-2}"
    fi
    export "$key=$value"
  done < "$env_file"
}
load_dotenv_file "${REPO_ROOT}/.env"

if [ ! -x "${PYTHON_BIN}" ]; then
  echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] FATAL: venv python not found at ${PYTHON_BIN}" >> "${LOG_FILE}"
  exit 3
fi

{
  echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] pattern_loop_reconstruct.sh starting"
} >> "${LOG_FILE}"

"${PYTHON_BIN}" - >> "${LOG_FILE}" 2>&1 <<'PY'
import glob, json
from portfolio_automation.historical_backfill import run_historical_backfill
from backtesting.historical_signal_recon import (
    reconstruct_universe, assert_no_lookahead, write_reconstruction_audit,
)

bf = run_historical_backfill(root=".")
print("backfill:", bf.get("status") if isinstance(bf, dict) else bf)

summary = reconstruct_universe("outputs/backtest/historical", "outputs/backtest/recon")
print("recon:", json.dumps(summary))

series = {}
for f in glob.glob("outputs/backtest/historical/*_5y.json"):
    try:
        d = json.load(open(f))
        series[d.get("symbol", f)] = d.get("rows", [])
    except Exception:
        continue
audit = assert_no_lookahead(series, sample=8)
write_reconstruction_audit(audit)
print("lookahead_clean:", audit["look_ahead_clean"], "dates_checked:", audit["dates_checked"])
PY
RC1=$?

echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] reconstruction done rc=${RC1}; running loop over recon history" >> "${LOG_FILE}"
"${PYTHON_BIN}" -m backtesting.run_loop --history outputs/backtest/recon --live >> "${LOG_FILE}" 2>&1
RC2=$?

echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] pattern_loop_reconstruct.sh done recon_rc=${RC1} loop_rc=${RC2}" >> "${LOG_FILE}"
[ "${RC1}" -eq 0 ] && [ "${RC2}" -eq 0 ]; exit $?
