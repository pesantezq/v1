#!/usr/bin/env bash
# Pattern-Loop notification watcher (OBSERVE-ONLY — monitoring, no mutation).
#
# Detects NEW autonomous auto-apply events (applied / rolled_back) since the last
# check and raises a prominent ALERT to logs/pattern_loop_alerts.log, then runs the
# full /pattern-loop-analysis skill for the rich readout. Scheduled to run shortly
# after the monthly reconstruct cron so any weight change the autonomous loop made is
# surfaced for operator review. Also tries an email via tools.notify_status's SMTP
# env if configured (degrades silently to the alert log otherwise).
#
# Best-effort: logs and exits 0 even on partial failure (a watcher must not crash).

set -uo pipefail

export HOME="${HOME:-/root}"
export PATH="/root/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

REPO_ROOT="/opt/stockbot"
cd "${REPO_ROOT}" || { echo "FATAL: cannot cd to ${REPO_ROOT}" >&2; exit 2; }

PYTHON_BIN="${REPO_ROOT}/.venv/bin/python"
LOG_DIR="${REPO_ROOT}/logs"
mkdir -p "${LOG_DIR}"
LOG_FILE="${LOG_DIR}/pattern_loop_check_$(date -u +%Y-%m).log"
ALERT_LOG="${LOG_DIR}/pattern_loop_alerts.log"

load_dotenv_file() {
  local env_file="$1" line trimmed key value
  [ -f "$env_file" ] || return 0
  while IFS= read -r line || [ -n "$line" ]; do
    line="${line%$'\r'}"; trimmed="${line#"${line%%[![:space:]]*}"}"
    [ -z "$trimmed" ] && continue; [ "${trimmed:0:1}" = "#" ] && continue
    trimmed="${trimmed#export }"; [[ "$trimmed" != *=* ]] && continue
    key="${trimmed%%=*}"; value="${trimmed#*=}"
    if [[ "$value" =~ ^\".*\"$ ]] || [[ "$value" =~ ^\'.*\'$ ]]; then value="${value:1:${#value}-2}"; fi
    export "$key=$value"
  done < "$env_file"
}
load_dotenv_file "${REPO_ROOT}/.env"

[ -x "${PYTHON_BIN}" ] || { echo "[$(date -u +%FT%TZ)] FATAL: venv python missing" >> "${LOG_FILE}"; exit 0; }

# --- deterministic detector: new applied/rolled_back events since last check -------------
"${PYTHON_BIN}" - <<'PY' >> "${LOG_FILE}" 2>&1
import json, os
from datetime import datetime, timezone

AUDIT = "outputs/policy/auto_apply_audit.json"
STATE = "data/pattern_loop_check_state.json"
ALERT = "logs/pattern_loop_alerts.log"

def load(path, default):
    try:
        return json.load(open(path))
    except Exception:
        return default

audit = load(AUDIT, [])
if not isinstance(audit, list):
    audit = []
consequential = [e for e in audit if isinstance(e, dict) and e.get("status") in ("applied", "rolled_back")]
state = load(STATE, {})
seen = int(state.get("seen_consequential", 0)) if isinstance(state, dict) else 0
new = consequential[seen:]

ts = datetime.now(timezone.utc).isoformat()
if new:
    lines = [f"[{ts}] PATTERN-LOOP ALERT: {len(new)} new auto-apply event(s)"]
    for e in new:
        chg = e.get("changes") or e.get("change") or e.get("applied")
        lines.append(f"    status={e.get('status')} changes={json.dumps(chg)} ts={e.get('ts')}")
    block = "\n".join(lines) + "\n"
    with open(ALERT, "a") as fh:
        fh.write(block)
    print(block.rstrip())
    print("NOTIFY=1")
else:
    print(f"[{ts}] pattern-loop watcher: no new auto-apply events (seen={seen}).")
    print("NOTIFY=0")

os.makedirs("data", exist_ok=True)
json.dump({"seen_consequential": len(consequential), "checked_at": ts}, open(STATE, "w"))
PY

# --- rich readout via the analysis skill (best-effort; needs claude CLI) ------------------
CLAUDE_BIN="$(command -v claude || true)"
if [[ -n "${CLAUDE_BIN}" ]]; then
  echo "[$(date -u +%FT%TZ)] invoking claude --print /pattern-loop-analysis" >> "${LOG_FILE}"
  "${CLAUDE_BIN}" --print "/pattern-loop-analysis" >> "${LOG_FILE}" 2>&1 || \
    echo "[$(date -u +%FT%TZ)] WARN: pattern-loop-analysis skill run failed" >> "${LOG_FILE}"
else
  echo "[$(date -u +%FT%TZ)] claude CLI absent — deterministic alert only" >> "${LOG_FILE}"
fi

echo "[$(date -u +%FT%TZ)] pattern_loop_check.sh done" >> "${LOG_FILE}"
exit 0
