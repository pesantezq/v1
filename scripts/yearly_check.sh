#!/usr/bin/env bash
# Yearly tool analysis cron wrapper.
#
# Runs at 10:00 UTC on January 1 (~24h after the year's first daily cron).
# Invokes `claude --print /yearly-tool-analysis` to dispatch the agent
# stack and produce docs/yearly_reports/YYYY.md — the annual operator
# review document.
#
# Also runnable on-demand for mid-year audits.

set -uo pipefail

export HOME="${HOME:-/root}"
export PATH="/root/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

REPO_ROOT="/opt/stockbot"
cd "${REPO_ROOT}" || { echo "FATAL: cannot cd to ${REPO_ROOT}" >&2; exit 2; }

LOG_DIR="${REPO_ROOT}/logs"
mkdir -p "${LOG_DIR}"
LOG_FILE="${LOG_DIR}/yearly_check_$(date -u +%Y).log"

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
  echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] yearly_check.sh starting"
  echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] invoking claude --print /yearly-tool-analysis"
} >> "${LOG_FILE}"

"${CLAUDE_BIN}" --print "/yearly-tool-analysis" >> "${LOG_FILE}" 2>&1
RC=$?

echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] yearly_check.sh done exit=${RC}" >> "${LOG_FILE}"
exit "${RC}"
