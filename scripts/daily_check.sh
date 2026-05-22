#!/usr/bin/env bash
# Daily Portfolio Automation health check (VPS-local cron variant).
#
# Runs at 09:15 UTC Mon-Fri, ~15 min after the 09:00 UTC production cron.
# Two-stage hybrid design:
#   1) Deterministic Python triage (portfolio_automation.daily_check_runner)
#      — always runs, no LLM dependency, no API key needed. Writes
#      daily_checks/YYYY-MM-DD.md and updates data/daily_check_state.json.
#   2) If stage 1 returns RED, optionally invoke `claude --print
#      /daily-portfolio-check` to append LLM-driven agent dispatch +
#      config-change proposals to the report. Best-effort; auth failures
#      degrade gracefully (the Python report is still on disk).
#
# Observe-only by design: mutates only daily_checks/* and the state file.

set -uo pipefail

export HOME="${HOME:-/root}"
export PATH="/root/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

REPO_ROOT="/opt/stockbot"
cd "${REPO_ROOT}" || { echo "FATAL: cannot cd to ${REPO_ROOT}" >&2; exit 2; }

# Mirror the env-loading pattern in scripts/run_daily_safe.sh so the cron
# subshell sees the same secrets (ANTHROPIC_API_KEY, FMP_API_KEY, etc.) the
# 09:00 UTC production cron uses. Without this, `claude --print` can't
# authenticate from cron context.
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
    # strip optional surrounding quotes
    if [[ "$value" =~ ^\".*\"$ ]] || [[ "$value" =~ ^\'.*\'$ ]]; then
      value="${value:1:${#value}-2}"
    fi
    export "$key=$value"
  done < "$env_file"
}
load_dotenv_file "${REPO_ROOT}/.env"

DATE_UTC="$(date -u +%Y-%m-%d)"
TS_UTC() { date -u +%Y-%m-%dT%H:%M:%SZ; }

REPORT_DIR="${REPO_ROOT}/daily_checks"
REPORT_FILE="${REPORT_DIR}/${DATE_UTC}.md"
LOG_DIR="${REPO_ROOT}/logs"
LOG_FILE="${LOG_DIR}/daily_check_${DATE_UTC}.log"

mkdir -p "${REPORT_DIR}" "${LOG_DIR}"

{
  echo "[$(TS_UTC)] daily_check.sh starting"
  echo "[$(TS_UTC)] repo=${REPO_ROOT} report=${REPORT_FILE}"
} >> "${LOG_FILE}"

# ---- Stage 1: deterministic Python triage ----
python3 -m portfolio_automation.daily_check_runner >> "${LOG_FILE}" 2>&1
PY_EXIT=$?
echo "[$(TS_UTC)] python stage exit=${PY_EXIT}" >> "${LOG_FILE}"

if [[ ! -s "${REPORT_FILE}" ]]; then
  echo "[$(TS_UTC)] FATAL: python stage produced no report file" >> "${LOG_FILE}"
  exit 3
fi

# Stage 1 exits 2 when verdict is RED (per daily_check_runner.main), 0 otherwise.
VERDICT="$(head -n 1 "${REPORT_FILE}" | sed -n 's/^\[\(GREEN\|AMBER\|RED\)\].*/\1/p')"
echo "[$(TS_UTC)] verdict=${VERDICT:-unknown}" >> "${LOG_FILE}"

# ---- Stage 2: optional LLM dispatch on RED ----
if [[ "${VERDICT}" == "RED" ]]; then
  CLAUDE_BIN="$(command -v claude || true)"
  if [[ -z "${CLAUDE_BIN}" ]]; then
    echo "[$(TS_UTC)] claude CLI not on PATH; skipping stage-2 LLM dispatch" >> "${LOG_FILE}"
  else
    echo "[$(TS_UTC)] stage-2: invoking claude --print for RED follow-up" >> "${LOG_FILE}"

    # Best-effort. If auth fails we append the error to the report so the
    # operator sees it, but the Python report stays intact.
    APPENDIX_FILE="$(mktemp)"
    PROMPT="The deterministic daily check (portfolio_automation.daily_check_runner) "
    PROMPT+="emitted a RED verdict for today (${DATE_UTC}). Read "
    PROMPT+="\`${REPORT_FILE}\` for the heartbeat + body, then follow Steps 2-3 of "
    PROMPT+="\`.claude/commands/daily-portfolio-check.md\`: dispatch the relevant "
    PROMPT+="agents and, if the RED action is in the auto-proposable whitelist "
    PROMPT+="(budget exhausted, or delta_hit_rate_pp<=-10 AND n>=30), open a PR "
    PROMPT+="from a daily-check-proposals/${DATE_UTC}-<slug> branch with the "
    PROMPT+="suggested config change. Do NOT push to main. Do NOT modify any "
    PROMPT+="protected-semantics surface. Output: a short summary of agent "
    PROMPT+="findings + PR/branch link or 'none'."

    if "${CLAUDE_BIN}" --print "${PROMPT}" > "${APPENDIX_FILE}" 2>>"${LOG_FILE}"; then
      {
        echo ""
        echo "---"
        echo ""
        echo "## LLM follow-up (stage 2)"
        echo ""
        cat "${APPENDIX_FILE}"
      } >> "${REPORT_FILE}"
      echo "[$(TS_UTC)] stage-2: appendix written" >> "${LOG_FILE}"
    else
      C_EXIT=$?
      {
        echo ""
        echo "---"
        echo ""
        echo "## LLM follow-up (stage 2)"
        echo ""
        echo "_claude --print exited ${C_EXIT}. Likely auth issue. See ${LOG_FILE}._"
        if [[ -s "${APPENDIX_FILE}" ]]; then
          echo ""
          echo "Partial output:"
          echo ""
          cat "${APPENDIX_FILE}"
        fi
      } >> "${REPORT_FILE}"
      echo "[$(TS_UTC)] stage-2: claude --print failed exit=${C_EXIT}" >> "${LOG_FILE}"
    fi
    rm -f "${APPENDIX_FILE}"
  fi
fi

echo "[$(TS_UTC)] daily_check.sh done verdict=${VERDICT:-unknown}" >> "${LOG_FILE}"

# Surface the verdict via the exit code (0=GREEN, 1=AMBER, 2=RED) for cron
# tooling that wants to alert on it.
case "${VERDICT}" in
  GREEN) exit 0 ;;
  AMBER) exit 1 ;;
  RED)   exit 2 ;;
  *)     exit 3 ;;
esac
