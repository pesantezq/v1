#!/usr/bin/env bash
# Monthly doc-audit cron entrypoint.
#
# Mirrors /doc-audit-monthly skill Step 1 (deterministic producer, report-only).
# The judgment step (Step 2 — portfolio-doc-auditor agent dispatch) requires an
# interactive Claude Code session and CANNOT be run headlessly: no existing
# wrapper in this repo invokes the `claude` CLI non-interactively, and the
# judgment dimensions (clarity, conciseness, redundancy, decomposition) require
# an LLM agent in context. This wrapper logs that fact explicitly so the cron
# output reminds the operator to follow up.
#
# What this wrapper DOES:
#   - Refreshes outputs/latest/doc_audit_status.json (producer context)
#   - Prints producer summary (status / findings / coverage gaps)
#   - Logs a reminder to run `/doc-audit-monthly` interactively for the
#     judgment review and portfolio-doc-auditor dispatch
#
# Lock file: /var/lock/stockbot-doc-audit.lock (shared with weekly wrapper)

set -euo pipefail

REPO_ROOT="${REPO_ROOT:-/opt/stockbot}"
LOG_DIR="$REPO_ROOT/logs"
mkdir -p "$LOG_DIR"

LOG_FILE="$LOG_DIR/doc_audit_monthly_$(date -u +%Y-%m-%d).log"

# Lock-file gating — prefer /var/lock; fall back to /tmp
if [ -d /var/lock ] && [ -w /var/lock ]; then
    LOCK_FILE="/var/lock/stockbot-doc-audit.lock"
else
    LOCK_FILE="/tmp/stockbot-doc-audit.lock"
fi

exec 9>"$LOCK_FILE"
if ! flock -n 9; then
    printf '%s run_doc_audit_monthly: lock held by another process — skipping\n' \
        "$(date -u +%Y-%m-%dT%H:%M:%SZ)" >> "$LOG_FILE"
    exit 0
fi

cd "$REPO_ROOT"

if [ ! -f "$REPO_ROOT/.venv/bin/activate" ]; then
    printf '%s run_doc_audit_monthly: venv missing at %s/.venv — aborting\n' \
        "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$REPO_ROOT" >> "$LOG_FILE"
    exit 1
fi

# shellcheck disable=SC1091
source "$REPO_ROOT/.venv/bin/activate"

{
    printf '\n=== run_doc_audit_monthly run @ %s ===\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    printf 'Repo root: %s\n' "$REPO_ROOT"

    printf '\n-- Step 1: Run deterministic producer (report-only, NO auto-fix) --\n'
    python3 - <<'PY'
import glob, json
from portfolio_automation import doc_audit, doc_audit_state

last = doc_audit_state.load_state('.')['last_audited_sha']
existing = set(glob.glob('docs/**/*.md', recursive=True))
# Monthly: pass empty changed list — audit full corpus, not just recent diff
result = doc_audit.run_doc_audit('.', last, [], existing)
doc_audit.write_doc_audit_status(result, '.')
print(json.dumps({
    "status": result["overall_status"],
    "findings": len(result["findings"]),
    "auto_fix_candidates": len(result["auto_fix_candidates"]),
    "gaps": len(result["coverage_gaps"])
}, indent=2))
PY

    printf '\n-- Producer complete. Artifact: outputs/latest/doc_audit_status.json --\n'

    printf '\n-- Step 2 (INTERACTIVE REQUIRED): Judgment review --\n'
    printf 'The portfolio-doc-auditor agent (clarity/conciseness/redundancy/decomposition)\n'
    printf 'requires an interactive Claude Code session and cannot run headlessly.\n'
    printf 'ACTION: run `/doc-audit-monthly` in a Claude Code session on this host to\n'
    printf 'dispatch portfolio-doc-auditor and receive the ranked judgment findings.\n'
    printf 'The producer artifact above is already fresh for that session.\n'

    printf '\n=== run_doc_audit_monthly PASSED (producer only) @ %s ===\n' \
        "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
} >> "$LOG_FILE" 2>&1
