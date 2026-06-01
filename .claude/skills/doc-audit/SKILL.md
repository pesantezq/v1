---
name: doc-audit
description: Weekly documentation audit. Runs the doc_audit producer over the corpus, auto-fixes high-confidence factual drift under guardrails (cap 10/run, apply_enabled flag), dispatches portfolio-doc-writer for the rest, and advances committed state. Runs on demand from any workstation and via VPS cron. Observe-by-default; only the guardrailed drift-fix mutates docs.
---

# Skill: doc-audit (weekly tier)

Working dir: `/opt/stockbot`. Python is `python3`.

## Step 1 — Resolve the git range

```bash
LAST_SHA=$(python3 -c "from portfolio_automation.doc_audit_state import load_state; print(load_state('.')['last_audited_sha'] or '')")
if [ -n "$LAST_SHA" ]; then RANGE="$LAST_SHA..HEAD"; else RANGE="HEAD~20..HEAD"; fi
git diff --name-only $RANGE > /tmp/doc_audit_changed.txt
```

## Step 2 — Run the producer

```bash
python3 - <<'PY'
import glob, json
from portfolio_automation import doc_audit, doc_audit_state
last = doc_audit_state.load_state('.')['last_audited_sha']
changed = [l.strip() for l in open('/tmp/doc_audit_changed.txt') if l.strip()]
existing = set(glob.glob('docs/**/*.md', recursive=True))
result = doc_audit.run_doc_audit('.', last, changed, existing)
doc_audit.write_doc_audit_status(result, '.')
print(json.dumps({"status": result["overall_status"],
                  "findings": len(result["findings"]),
                  "auto": len(result["auto_fix_candidates"]),
                  "gaps": len(result["coverage_gaps"])}))
PY
```

## Step 3 — Triage

Read `outputs/latest/doc_audit_status.json`.
- **GREEN** — `overall_status == "ok"`, no findings. Emit heartbeat, stop.
- **AMBER** — `overall_status` is `drift` or `ok_with_warnings`; auto-fixes available, no coverage gap.
- **RED** — `overall_status == "coverage_gap"` (a shipped change has no doc) OR any `consistency` finding with severity `high`.

## Step 4 — Apply guardrailed auto-fixes (only if apply_enabled)

Guardrails: only `auto_fix_candidates`; cap 10 per run; skip entirely if `apply_enabled` is false.

```bash
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
            ('dimension','severity','doc','detail','auto_fixable','anchor','current','expected','line')})
        if doc_audit.apply_auto_fix(f, '.'):
            applied.append({"doc": f.doc, "anchor": f.anchor, "from": f.current, "to": f.expected})
json.dump(applied, open('/tmp/doc_audit_applied.json','w'))
print(json.dumps(applied))
PY
```

If any fixes were applied, commit them in a dedicated commit:

```bash
git add docs/ && git commit -m "docs(auto): doc-audit drift fixes $(date -u +%F)" || echo "no doc changes"
```

Rollback if wrong: `git revert <that commit>`.

## Step 5 — Dispatch portfolio-doc-writer for the rest

For every finding that is NOT auto-fixable (dead_ref, coverage, consistency), dispatch the
`portfolio-doc-writer` agent with the finding list so it can draft doc updates for operator
approval. Do NOT auto-commit the writer's edits.

## Step 6 — Advance committed state

```bash
python3 - <<'PY'
import json, os, subprocess
from datetime import datetime, timezone
from portfolio_automation import doc_audit_state
applied = json.load(open('/tmp/doc_audit_applied.json')) if os.path.exists('/tmp/doc_audit_applied.json') else []
head = subprocess.check_output(['git','rev-parse','HEAD']).decode().strip()
st = doc_audit_state.load_state('.')
st['last_audited_sha'] = head
st['last_run_at'] = datetime.now(timezone.utc).isoformat()
st['fixes_last_run'] = len(applied)
doc_audit_state.save_state('.', st)
PY
git add .agent/doc_audit_state.yaml && git commit -m "chore(doc-audit): advance audit state $(date -u +%F)" || echo "state unchanged"
```

## Step 7 — Heartbeat output

`[GREEN|AMBER|RED] doc-audit YYYY-MM-DD: N findings, M auto-fixed, K coverage gaps`
Then list each coverage gap + dead-ref so the operator sees what needs a doc.

## Push note

This skill commits locally; it does NOT push. Push when you next sync, or add `git push`
to the VPS cron wrapper for hands-off remote sync.
