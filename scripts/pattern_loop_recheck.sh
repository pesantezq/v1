#!/usr/bin/env bash
# Pattern-Improvement Loop monthly recompute (observe-only, proposes-only).
#
# Runs `run_loop --history --live` so signal_weight_proposals.json and
# poc_simulation_results.json (incl. the oos_window maturity block) stay fresh
# as signal history accumulates. FMP-only (free); no AI/LLM spend. Step 5
# (apply) is never invoked — this only ever proposes.
#
# Best-effort: logs and exits non-zero on failure; the caller treats it as
# non-blocking. Intended to be invoked by monthly_check.sh before the analysis.

set -uo pipefail

export HOME="${HOME:-/root}"
export PATH="/root/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

REPO_ROOT="/opt/stockbot"
cd "${REPO_ROOT}" || { echo "FATAL: cannot cd to ${REPO_ROOT}" >&2; exit 2; }

PYTHON_BIN="${REPO_ROOT}/.venv/bin/python"
LOG_DIR="${REPO_ROOT}/logs"
mkdir -p "${LOG_DIR}"
LOG_FILE="${LOG_DIR}/pattern_loop_recheck_$(date -u +%Y-%m).log"

# Load .env (same minimal parser as monthly_check.sh) so FMP_API_KEY is set.
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
  echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] pattern_loop_recheck.sh starting"
  echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] invoking run_loop --history --live"
} >> "${LOG_FILE}"

"${PYTHON_BIN}" -m backtesting.run_loop --history --live >> "${LOG_FILE}" 2>&1
RC=$?

echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] pattern_loop_recheck.sh done exit=${RC}" >> "${LOG_FILE}"
exit "${RC}"
