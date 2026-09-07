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

# --- Git authorship gate --------------------------------------------------
# This script used to be scheduled by production cron (Mondays 07:00 UTC) and
# was therefore a git AUTHOR on the production host: it produced ten unpushed
# commits between 2026-08-10 and 2026-09-07, including commits to .agent/.
# That made `production_code_sha == approved_release_sha` decay weekly and put
# an autonomous committer inside the protected namespace.
#
# The deployment contract is now PRODUCTION_RUNTIME_DOES_NOT_CREATE_GIT_COMMITS
# (see docs/PRODUCTION_RELEASE_CONTRACT.md). Repository authorship is opt-in:
# the audit, its status artifact and its auto-fixes all still run, but the
# commits happen only when an engineering/control plane asks for them. An
# accidental production invocation now audits and reports instead of
# authoring, which is the safe failure direction.
#
#   STOCKBOT_DOC_AUDIT_GIT_AUTHORSHIP=1   engineering / QPC worker / CI
#   unset or 0                            everywhere else (default)
DOC_AUDIT_GIT_AUTHORSHIP="${STOCKBOT_DOC_AUDIT_GIT_AUTHORSHIP:-0}"

doc_audit_commit() {
    # Only ever reached in authoring mode: a non-authoring run exits before
    # Step 4, so nothing is applied, staged or committed in the first place.
    #
    # An earlier version of this gate let the mutations happen and then ran
    # `git reset` on the index. That was wrong twice over: the index reset does
    # not undo the file writes, so the worktree was left dirty, and unstaging is
    # not evidence that the tree is unchanged. Mutation is now gated BEFORE it
    # occurs, and this assertion fails loudly rather than silently committing if
    # that control flow ever changes.
    if [ "$DOC_AUDIT_GIT_AUTHORSHIP" != "1" ]; then
        printf 'FATAL: commit attempted without git authorship: %s\n' "$1" >&2
        exit 2
    fi
    git commit -m "$1" || printf 'Commit produced no change.\n'
}
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
import glob, json, os
from portfolio_automation import doc_audit, doc_audit_state

last = doc_audit_state.load_state('.')['last_audited_sha']
changed = [l.strip() for l in open('/tmp/doc_audit_changed.txt') if l.strip()]
existing = set(glob.glob('docs/**/*.md', recursive=True))
result = doc_audit.run_doc_audit('.', last, changed, existing)
# write_doc_audit_status persists coverage-gap bookkeeping into the TRACKED
# .agent/doc_audit_state.yaml. Without authorship this run must mutate zero
# tracked files, so the artifact is written and the state write is skipped.
authoring = os.environ.get('STOCKBOT_DOC_AUDIT_GIT_AUTHORSHIP') == '1'
doc_audit.write_doc_audit_status(result, '.', persist_state=authoring)
print(json.dumps({
    "status": result["overall_status"],
    "findings": len(result["findings"]),
    "auto": len(result["auto_fix_candidates"]),
    "gaps": len(result["coverage_gaps"])
}, indent=2))
PY

    # --- Mutation boundary -------------------------------------------------
    # Everything above is read-only: it resolves a git range, runs the auditor,
    # and writes outputs/latest/doc_audit_status.json, which is an ignored
    # runtime artifact (see .gitignore "outputs/latest/"). Everything below
    # MUTATES tracked repository state — doc auto-fixes, generated docs, and
    # .agent/doc_audit_state.yaml.
    #
    # PRODUCTION_RUNTIME_DOES_NOT_CREATE_GIT_COMMITS, and more strictly
    # PRODUCTION_RUNTIME_MUTATES_ZERO_TRACKED_RELEASE_FILES, so a run without
    # explicit authorship stops here having changed no tracked path, no index
    # entry and no ref. It still reports what an engineering run would fix.
    if [ "$DOC_AUDIT_GIT_AUTHORSHIP" != "1" ]; then
        printf '\n-- NON-AUTHORING MODE: stopping before any tracked mutation --\n'
        printf 'STOCKBOT_DOC_AUDIT_GIT_AUTHORSHIP != 1\n'
        printf 'Audit ran and the status artifact was written (untracked).\n'
        printf 'NOT applied: doc auto-fixes, generated-doc rewrites, '
        printf 'doc_audit_state advance, staging, commits.\n'
        python3 - <<'PY'
import json
from pathlib import Path

path = Path('outputs/latest/doc_audit_status.json')
if not path.exists():
    print('No status artifact to report.')
else:
    result = json.loads(path.read_text(encoding='utf-8'))
    cands = result.get('auto_fix_candidates') or []
    print(f"overall_status : {result.get('overall_status')}")
    print(f"findings       : {len(result.get('findings') or [])}")
    print(f"coverage_gaps  : {len(result.get('coverage_gaps') or [])}")
    print(f"auto-fixable   : {len(cands)} (suggested, NOT applied)")
    for c in cands[:20]:
        doc = c.get('doc') if isinstance(c, dict) else c
        print(f"  would fix: {doc}")
    if len(cands) > 20:
        print(f"  ... and {len(cands) - 20} more")
    print()
    print('To apply and commit these, run from an engineering plane with:')
    print('  STOCKBOT_DOC_AUDIT_GIT_AUTHORSHIP=1 scripts/run_doc_audit.sh')
PY
        printf '\n=== run_doc_audit PASSED (non-authoring, read-only) @ %s ===\n' \
            "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
        exit 0
    fi

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
    # Commit ONLY the docs the auditor actually rewrote.
    #
    # The previous blanket `git add docs/` swept in whatever else happened to be
    # dirty. Every `docs(auto): doc-audit drift fixes` commit from 2026-06-15
    # through 2026-08-03 carried pipeline-regenerated files (STRATEGY_CATALOG.md,
    # monthly_reports/*, ALLOCATION_POLICY.md) while `fixes_last_run` was 0 at
    # every single state advance — i.e. the auditor claimed authorship of output
    # it never touched. Wrong provenance on an autonomous committer. (2026-08-08)
    FIX_COUNT=$(python3 -c "
import json
print(len(json.load(open('/tmp/doc_audit_applied.json'))))
")
    if [ "$FIX_COUNT" -gt 0 ]; then
        python3 -c "
import json
seen = set()
for f in json.load(open('/tmp/doc_audit_applied.json')):
    if f['doc'] not in seen:
        seen.add(f['doc'])
        print(f['doc'])
" > /tmp/doc_audit_fix_paths.txt
        xargs -a /tmp/doc_audit_fix_paths.txt -r git add --
        doc_audit_commit "docs(auto): doc-audit drift fixes $(date -u +%F) (${FIX_COUNT} anchors)"
    else
        printf 'No auto-fixes applied — nothing to commit under doc-audit provenance.\n'
    fi

    printf '\n-- Commit pipeline-generated docs (separate provenance) --\n'
    # Generated docs are rewritten by their own producers (the daily pipeline
    # regenerates STRATEGY_CATALOG.md at 09:45; the monthly report writer emits
    # monthly_reports/*). Historically they reached git ONLY as a side effect of
    # the sweep above — `docs/STRATEGY_CATALOG.md` has never been committed by
    # anything else. Keep committing them so they don't sit dirty forever, but
    # under a message that says what they are.
    #
    # Discovered dynamically from the dirty set — no hardcoded file list, so a
    # newly-added generated doc is picked up automatically.
    #
    # Deliberately limited to MODIFIED TRACKED files: an untracked doc under
    # docs/ is new authored content (e.g. a portfolio-doc-writer draft from
    # Step 5) and must stay for human review, never be auto-committed by cron.
    if [ -n "$(git diff --name-only -- docs/)" ]; then
        git diff --name-only -- docs/ | sed 's/^/  /'
        git add --update -- docs/
        doc_audit_commit "docs(generated): pipeline-regenerated docs $(date -u +%F)"
    else
        printf 'No modified generated docs.\n'
    fi

    UNTRACKED_DOCS=$(git ls-files --others --exclude-standard -- docs/)
    if [ -n "$UNTRACKED_DOCS" ]; then
        printf 'Untracked docs left for human review (NOT auto-committed):\n%s\n' \
            "$(printf '%s\n' "$UNTRACKED_DOCS" | sed 's/^/  /')"
    fi

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
    git add .agent/doc_audit_state.yaml
    doc_audit_commit "chore(doc-audit): advance audit state $(date -u +%F)"

    printf '\n=== run_doc_audit PASSED @ %s ===\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
} >> "$LOG_FILE" 2>&1
