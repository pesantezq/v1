# Task template — Documentation cleanup (stale / old docs)

Curate and retire stale documentation, building on the existing **doc-audit
system** (don't reinvent it). Archive rather than hard-delete by default.
Paste the block below into Claude Code from the repo root.

---

```
You are working in this advisory-only repo. Obey CLAUDE.md and AGENTS.md exactly.

Read first:
- CLAUDE.md and AGENTS.md
- docs/doc_audit.md  (the existing observe-only documentation auditor)
- docs/ARCHITECTURE_MAP.md and docs/BUILD_ROADMAP.md (the current doc map)
- .agent/project_state.yaml

Objective: reduce documentation drift and retire stale/superseded docs, using
the doc-audit system as the evidence base. Documentation-only — never modify
scoring/decision/allocation logic or output schemas.

Begin in PLAN MODE. Produce the findings + a curation plan, and WAIT for my
approval before moving or editing any doc.

1. Run the auditor: `bash scripts/run_doc_audit.sh` (or
   `python -m portfolio_automation.doc_audit`). Summarize its four families:
   factual drift, coverage gaps, dead refs, cross-doc inconsistency.
2. Fix the safe, anchor-bound factual drift the auditor flags as auto-fixable;
   list (do not auto-fix) the ambiguous ones for my review.
3. Identify STALE / SUPERSEDED docs the auditor can't judge on its own — e.g.,
   docs describing completed or replaced tracks (such as the Streamlit operator
   cockpit superseded by gui_v2). For each, propose: keep / consolidate / archive.
4. For archive: MOVE to docs/archive/ (don't hard-delete), add a one-line
   "Superseded by X (date)" banner at the top, and update inbound links/refs so
   nothing breaks. This is additive and reversible.
5. Close coverage gaps the auditor reports (new source module with no docs/<stem>.md)
   by drafting a short stub or noting it for the portfolio-doc-writer.
6. Re-run the auditor; confirm dead-refs and drift counts dropped and nothing
   new broke.

End with the repo's Final Report listing docs kept/consolidated/archived and the
before/after auditor counts. PAUSE for my approval before deleting anything.
If on the laptop, return VPS validation commands as a copyable block.
```
