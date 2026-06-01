---
name: doc-audit-monthly
description: Monthly documentation retrospective. Runs the deterministic doc_audit producer for context, then dispatches the read-only portfolio-doc-auditor agent for the judgment dimensions (clarity, conciseness, redundancy, large-doc decomposition). Report-only — accepted findings are handed to portfolio-doc-writer. Runs on demand anywhere and via VPS cron on the 1st.
---

# Skill: doc-audit-monthly (judgment tier)

Working dir: `/opt/stockbot`. Python is `python3`.

## Step 1 — Run the deterministic producer for context (report-only, NO auto-fix)

```bash
python3 - <<'PY'
import glob, json
from portfolio_automation import doc_audit, doc_audit_state
last = doc_audit_state.load_state('.')['last_audited_sha']
existing = set(glob.glob('docs/**/*.md', recursive=True))
result = doc_audit.run_doc_audit('.', last, [], existing)
doc_audit.write_doc_audit_status(result, '.')
print(json.dumps({"status": result["overall_status"],
                  "findings": len(result["findings"]),
                  "gaps": len(result["coverage_gaps"])}))
PY
```

This refreshes `outputs/latest/doc_audit_status.json`. Do NOT run the weekly skill's Step 4 auto-fix here — the monthly tier is report-only.

## Step 2 — Dispatch the judgment lens

Dispatch the `portfolio-doc-auditor` agent. Pass it the path `outputs/latest/doc_audit_status.json`
and ask for a ranked clarity / conciseness / redundancy / decomposition review of the
`docs/**/*.md` corpus. If the agent is not yet live (newly committed, pre session-restart),
note that and emit producer-only findings.

## Step 3 — Monthly heartbeat

`[GREEN|AMBER|RED] doc-audit-monthly YYYY-MM: <headline>`
Body: top 5 judgment findings + any standing coverage gaps from the producer.
Report-only: list what to hand to `portfolio-doc-writer`; do not edit any doc.
