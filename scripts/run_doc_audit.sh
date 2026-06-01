#!/usr/bin/env bash
# Weekly doc-audit cron entrypoint.
#
# Mirrors /doc-audit skill Steps 1-2-4-6:
#   1. Resolve git range from doc_audit_state.last_audited_sha (fallback HEAD~20..HEAD)
#   2. Run the deterministic producer (run_doc_audit + write_doc_audit_status)
#   4. Apply up to 10 guardrailed auto-fixes if apply_enabled; commit docs/ if changed
#   6. Advance .agent/doc_audit_state.yaml and commit
#
# Step 5 (portfolio-doc-writer dispatch) requires an interactive Claude session
# and is skipped here — run `/doc-audit` interactively after the cron to triage
# any non-auto-fixable findings.
#
# Lock file: /var/lock/stockbot-doc-audit.lock
# The crontab also wraps with `flock -n`, but we acquire it here too so the
# script is safe when invoked manually without the outer flock.

set -euo pipefail

REPO_ROOT="${REPO_ROOT:-/opt/stockbot}"
LOG_DIR="$REPO_ROOT/logs"
mkdir -p "$LOG_DIR"

LOG_FILE="$LOG_DIR/doc_audit_$(date -u +%Y-%m-%d).log"

# Lock-file gating — prefer /var/lock; fall back to /tmp
if [ -d /var/lock ] && [ -w /var/lock ]; then
    LOCK_FILE="/var/lock/stockbot-doc-audit.lock"
else
    LOCK_FILE="/tmp/stockbot-doc-audit.lock"
fi

exec 9>"$LOCK_FILE"
if ! flock -n 9; then
    printf '%s run_doc_audit: lock held by another process — skipping\n' \
        "$(date -u +%Y-%m-%dT%H:%M:%SZ)" >> "$LOG_FILE"
    exit 0
fi

cd "$REPO_ROOT"

if [ ! -f "$REPO_ROOT/.venv/bin/activate" ]; then
    printf '%s run_doc_audit: venv missing at %s/.venv — aborting\n' \
        "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$REPO_ROOT" >> "$LOG_FILE"
    exit 1
fi

# shellcheck disable=SC1091
source "$REPO_ROOT/.venv/bin/activate"

{
    printf '\n=== run_doc_audit (weekly) run @ %s ===\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    printf 'Repo root: %s\n' "$REPO_ROOT"

    printf '\n-- Step 1: Resolve git range --\n'
    LAST_SHA=$(python3 -c "
from portfolio_automation.doc_audit_state import load_state
print(load_state('.')['last_audited_sha'] or '')
")
    if [ -n "$LAST_SHA" ]; then
        RANGE="${LAST_SHA}..HEAD"
    else
        RANGE="HEAD~20..HEAD"
    fi
    printf 'Range: %s\n' "$RANGE"
    git diff --name-only "$RANGE" > /tmp/doc_audit_changed.txt
    printf 'Changed files: %s\n' "$(wc -l < /tmp/doc_audit_changed.txt)"

    printf '\n-- Step 2: Run producer --\n'
    python3 - <<'PY'
import glob, json
from portfolio_automation import doc_audit, doc_audit_state

last = doc_audit_state.load_state('.')['last_audited_sha']
changed = [l.strip() for l in open('/tmp/doc_audit_changed.txt') if l.strip()]
existing = set(glob.glob('docs/**/*.md', recursive=True))
result = doc_audit.run_doc_audit('.', last, changed, existing)
doc_audit.write_doc_audit_status(result, '.')
print(json.dumps({
    "status": result["overall_status"],
    "findings": len(result["findings"]),
    "auto": len(result["auto_fix_candidates"]),
    "gaps": len(result["coverage_gaps"])
}, indent=2))
PY

    printf '\n-- Step 4: Apply guardrailed auto-fixes --\n'
    python3 - <<'PY'
import json
from portfolio_automation import doc_audit, doc_audit_state
from portfolio_automation.doc_audit import Finding

st = doc_audit_state.load_state('.')
result = json.load(open('outputs/latest/doc_audit_status.json'))
applied = []
if st.get('apply_enabled', True):
    for fd in result['auto_fix_candidates'][:10]:
        f = Finding(**{k: fd.get(k) for k in
            ('dimension', 'severity', 'doc', 'detail', 'auto_fixable',
             'anchor', 'current', 'expected', 'line')})
        if doc_audit.apply_auto_fix(f, '.'):
            applied.append({"doc": f.doc, "anchor": f.anchor,
                            "from": f.current, "to": f.expected})
else:
    print('apply_enabled=false — skipping auto-fixes')
json.dump(applied, open('/tmp/doc_audit_applied.json', 'w'))
print(json.dumps({"fixes_applied": len(applied), "fixes": applied}, indent=2))
PY

    printf '\n-- Commit auto-fixed docs (if any) --\n'
    git add docs/ && git commit -m "docs(auto): doc-audit drift fixes $(date -u +%F)" \
        || printf 'No doc changes to commit.\n'

    printf '\n-- Step 6: Advance committed state --\n'
    python3 - <<'PY'
import json, os, subprocess
from datetime import datetime, timezone
from portfolio_automation import doc_audit_state

applied = json.load(open('/tmp/doc_audit_applied.json')) \
    if os.path.exists('/tmp/doc_audit_applied.json') else []
head = subprocess.check_output(['git', 'rev-parse', 'HEAD']).decode().strip()
st = doc_audit_state.load_state('.')
st['last_audited_sha'] = head
st['last_run_at'] = datetime.now(timezone.utc).isoformat()
st['fixes_last_run'] = len(applied)
doc_audit_state.save_state('.', st)
print(f'State advanced to HEAD={head[:12]}, fixes_last_run={len(applied)}')
PY
    git add .agent/doc_audit_state.yaml && \
        git commit -m "chore(doc-audit): advance audit state $(date -u +%F)" \
        || printf 'State unchanged — no commit needed.\n'

    printf '\n=== run_doc_audit PASSED @ %s ===\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
} >> "$LOG_FILE" 2>&1
