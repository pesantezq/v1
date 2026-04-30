# Skill: portfolio-feature

## Purpose

Implement a scoped feature for the Portfolio Automation System with tests,
docs, non-blocking pipeline integration, and a structured final report.

## When to Use

- Implementing a new observability module (data quality, AI budget, signal registry, etc.)
- Adding a new additive layer to the pipeline
- Implementing a step from `.agent/project_state.yaml:next_official_step`
- Building a new artifact writer + summary function

## When NOT to Use

- Changing scoring, allocation, or recommendation behavior (requires explicit user approval first)
- Starting Discovery Engine before it is in `next_official_step`
- Adding broker integration or auto-trading
- Building GUI pages (use a separate GUI-specific task)
- Running VPS deployment (the user does this manually)

## Step-by-Step Process

1. **Read project state**
   ```bash
   python scripts/agent_context_check.py
   cat .agent/project_state.yaml
   ```
   Confirm the requested step is in `next_official_step`. If not, stop and ask.

2. **Read relevant existing modules**
   - `portfolio_automation/data_governance.py` — namespace-aware write functions
   - Any module the new one will integrate with
   - `main.py` — find the integration point

3. **Implement the module**
   - Create `portfolio_automation/<module>.py`
   - Use `safe_write_json` / `safe_write_text` for all file writes
   - Hardcode `observe_only: True` in all artifact payloads
   - Follow the dataclass pattern: Config, Issue/Event, Summary dataclasses
   - Add a `write_<module>_report()` function that writes to the correct namespace

4. **Write tests**
   - Create `tests/test_<module>.py`
   - Cover: happy path, empty inputs, missing files, malformed data, observe_only behavior,
     blocking behavior (if applicable), namespace correctness, artifact fields

5. **Write module docs**
   - Create or update `docs/<MODULE_NAME>.md`
   - Follow the standard module doc template in `.claude/agents/portfolio-doc-writer.md`

6. **Integrate into main.py**
   - Add a `try/except`-wrapped section after the relevant existing block
   - Guard with `if not dry_run:` for artifact writes
   - Log with module name prefix: `logger.info("<MODULE>: %s", summary.summary_line)`

7. **Update roadmap**
   - Add a completion entry to `docs/roadmap.md`

8. **Compile and test**
   ```bash
   python -m py_compile portfolio_automation/<module>.py
   python -m pytest -q tests/test_<module>.py
   python -m pytest -q --ignore=tests/test_gui_api_health.py --ignore=tests/test_gui_insight_cards.py
   ```

9. **Return final report**
   Use `.agent/task_templates/final_report_template.md`.
   Include VPS validation commands. Do not claim VPS tests passed.

## Required Final Output

- New module file: `portfolio_automation/<module>.py`
- New test file: `tests/test_<module>.py`
- Updated or new docs: `docs/<MODULE_NAME>.md`
- Updated roadmap: `docs/roadmap.md`
- Updated main.py integration block
- Final report using the template, including VPS commands
