#!/usr/bin/env bash
# Monthly tool analysis cron wrapper.
#
# Runs at 09:30 UTC on the 1st of each month (~30 min after the daily
# cron completes for that day). Invokes `claude --print /monthly-tool-analysis`
# to dispatch the agent stack and produce docs/monthly_reports/YYYY-MM.md.
#
# Best-effort: if claude CLI auth fails the script logs and exits cleanly;
# the next month's run will pick up where this left off.

set -uo pipefail

export HOME="${HOME:-/root}"
export PATH="/root/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

REPO_ROOT="/opt/stockbot"
cd "${REPO_ROOT}" || { echo "FATAL: cannot cd to ${REPO_ROOT}" >&2; exit 2; }

LOG_DIR="${REPO_ROOT}/logs"
mkdir -p "${LOG_DIR}"
LOG_FILE="${LOG_DIR}/monthly_check_$(date -u +%Y-%m).log"

# Load .env via the same parser used in daily_check.sh
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

CLAUDE_BIN="$(command -v claude || true)"
if [[ -z "${CLAUDE_BIN}" ]]; then
  echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] FATAL: claude CLI not on PATH" >> "${LOG_FILE}"
  exit 3
fi

{
  echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] monthly_check.sh starting"
} >> "${LOG_FILE}"

# Refresh the Pattern-Loop artifacts (observe-only, FMP-only) so the analysis
# below reads a current poc_simulation_results.json + signal_weight_proposals.json.
# Non-blocking: a failure here must not stop the monthly analysis.
{
  echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] running pattern_loop_recheck.sh (non-blocking)"
} >> "${LOG_FILE}"
"${REPO_ROOT}/scripts/pattern_loop_recheck.sh" >> "${LOG_FILE}" 2>&1 \
  || echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] WARN: pattern_loop_recheck.sh failed; continuing" >> "${LOG_FILE}"

{
  echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] invoking claude --print /monthly-tool-analysis"
} >> "${LOG_FILE}"

# Run; output appends to the monthly log. The skill itself writes the
# canonical report to docs/monthly_reports/YYYY-MM.md.
"${CLAUDE_BIN}" --print "/monthly-tool-analysis" >> "${LOG_FILE}" 2>&1
RC=$?

echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] monthly_check.sh done exit=${RC}" >> "${LOG_FILE}"
exit "${RC}"
