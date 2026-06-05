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

# --- deterministic detector: new weight changes (owner-gated OR autonomous) -------------
# Watches BOTH ledgers so ANY registry mutation is caught — not just the autonomous path.
# Also reports the armed-state every run so oversight never silently assumes the loop is on/off.
RECON_CRON="$(crontab -l 2>/dev/null | grep -c 'pattern_loop_reconstruct.sh' || true)"
export PL_RECON_CRON="${RECON_CRON}"
"${PYTHON_BIN}" - <<'PY' >> "${LOG_FILE}" 2>&1
import json, os
from datetime import datetime, timezone

STATE = "data/pattern_loop_check_state.json"
ALERT = "logs/pattern_loop_alerts.log"
# (ledger_path, kind, status_field, predicate)
LEDGERS = [
    ("outputs/policy/auto_apply_audit.json", "autonomous",
     lambda e: e.get("status") in ("applied", "rolled_back")),
    ("outputs/policy/registry_apply_audit.json", "owner_gated",
     lambda e: e.get("applied_by") in ("apply", "revert")),
]

def load(path, default):
    try:
        return json.load(open(path))
    except Exception:
        return default

ts = datetime.now(timezone.utc).isoformat()
state = load(STATE, {})
if not isinstance(state, dict):
    state = {}
seen = state.get("seen") or {}

# armed-state — so the operator always knows whether the autonomous loop is live
enabled = bool(((load("config.json", {}) or {}).get("backtesting") or {}).get("auto_apply", {}).get("enabled", False))
recon_cron = (os.environ.get("PL_RECON_CRON", "0").strip() not in ("", "0"))
kill = os.path.exists("config/auto_apply.DISABLED")
armed = enabled and recon_cron and not kill
print(f"[{ts}] ARMED={armed} (enabled={enabled}, reconstruct_cron={recon_cron}, kill_switch={kill})")

new_events = []
new_seen = dict(seen)
for path, kind, pred in LEDGERS:
    ledger = load(path, [])
    if not isinstance(ledger, list):
        ledger = []
    consequential = [e for e in ledger if isinstance(e, dict) and pred(e)]
    prior = int(seen.get(kind, 0))
    for e in consequential[prior:]:
        new_events.append((kind, e))
    new_seen[kind] = len(consequential)

if new_events:
    lines = [f"[{ts}] PATTERN-LOOP ALERT: {len(new_events)} new registry weight change(s)"]
    for kind, e in new_events:
        chg = e.get("changes") or e.get("applied") or e.get("restored_from")
        who = e.get("approved_by") or e.get("applied_by") or kind
        lines.append(f"    [{kind}] {json.dumps(chg)} by={who} ts={e.get('ts')}")
    block = "\n".join(lines) + "\n"
    with open(ALERT, "a") as fh:
        fh.write(block)
    print(block.rstrip())
    print("NOTIFY=1")

    # Email push — reuses the existing memo-email SMTP plumbing + MEMO_EMAIL_* env.
    # No-op (skip/dry-run) unless the operator has configured + enabled email, so the
    # alert log above is always the reliable channel; email is best-effort on top.
    try:
        from email.message import EmailMessage
        from portfolio_automation.memo_email_sender import (
            load_memo_email_config, send_daily_memo_email,
        )
        cfg = load_memo_email_config()
        if cfg.enabled and cfg.has_smtp_config() and cfg.has_valid_recipients():
            msg = EmailMessage()
            prefix = (cfg.subject_prefix + " ") if cfg.subject_prefix else ""
            msg["Subject"] = f"{prefix}[StockBot] Pattern-Loop weight change ({len(new_events)})"
            msg["From"] = cfg.from_addr
            msg["To"] = ", ".join(cfg.to_addrs)
            if cfg.cc_addrs:
                msg["Cc"] = ", ".join(cfg.cc_addrs)
            msg.set_content(
                "The Pattern-Improvement Loop changed signal-registry weight(s).\n\n"
                + block
                + "\nReview: /pattern-loop-analysis  |  Undo: registry_apply.revert_last"
                + "  |  Halt autonomous: touch config/auto_apply.DISABLED\n")
            res = send_daily_memo_email(cfg, msg)
            print(f"EMAIL: sent={res.get('sent')} reason={res.get('reason') or res.get('error_class') or 'ok'}")
        else:
            print("EMAIL: skipped (MEMO_EMAIL_* not enabled/configured) — alert log is the channel")
    except Exception as exc:  # email must never break the watcher
        print(f"EMAIL: error (non-fatal): {exc}")
else:
    print(f"[{ts}] pattern-loop watcher: no new weight changes (seen={json.dumps(seen)}).")
    print("NOTIFY=0")

os.makedirs("data", exist_ok=True)
json.dump({"seen": new_seen, "armed": armed, "checked_at": ts}, open(STATE, "w"))
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
