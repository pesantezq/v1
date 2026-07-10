#!/usr/bin/env bash
# Cadence-suite runner — invokes a run-all-<cadence> super-skill headless.
#
# Usage: run_suite.sh <daily|weekly|monthly>
#
# Thin wrapper around `claude --print "/run-all-<cadence>"`, mirroring the
# env-loading pattern of scripts/monthly_check.sh so a cron subshell sees the
# same secrets (ANTHROPIC_API_KEY, FMP_API_KEY, ...) the production cron uses.
#
# Observe-only: the suite and its member skills mutate only their own
# report/state artifacts. This wrapper installs nothing and decides nothing.
#
# NOTE ON OVERLAP: the individual member skills are ALSO scheduled by their own
# cron entries (daily_check.sh, monthly_check.sh, run_doc_audit.sh,
# pattern_loop_check.sh, run_sims_daily.sh). Only schedule this wrapper if you
# intend to REPLACE those entries — otherwise members run twice (double LLM
# cost against the $20/mo cap). See docs/superpowers/specs/2026-07-10-cadence-
# suite-super-skills-design.md.

set -uo pipefail

CADENCE="${1:-}"
case "${CADENCE}" in
  daily|weekly|monthly) ;;
  *) echo "usage: $0 <daily|weekly|monthly>" >&2; exit 2 ;;
esac

export HOME="${HOME:-/root}"
export PATH="/root/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

REPO_ROOT="/opt/stockbot"
cd "${REPO_ROOT}" || { echo "FATAL: cannot cd to ${REPO_ROOT}" >&2; exit 2; }

# Load .env with the same parser used by daily_check.sh / monthly_check.sh.
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

LOG_DIR="${REPO_ROOT}/logs"
mkdir -p "${LOG_DIR}"
LOG_FILE="${LOG_DIR}/run_suite_${CADENCE}_$(date -u +%Y-%m-%d).log"

CLAUDE_BIN="$(command -v claude || true)"
if [[ -z "${CLAUDE_BIN}" ]]; then
  echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] FATAL: claude CLI not on PATH" >> "${LOG_FILE}"
  exit 3
fi

echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] run_suite.sh starting cadence=${CADENCE}" >> "${LOG_FILE}"
"${CLAUDE_BIN}" --print "/run-all-${CADENCE}" >> "${LOG_FILE}" 2>&1
RC=$?
echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] run_suite.sh done cadence=${CADENCE} exit=${RC}" >> "${LOG_FILE}"
exit "${RC}"
