# Dashboard / Mobile Polish Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`) syntax. EXECUTE IN AN ISOLATED GIT WORKTREE (concurrent `gui_v2` sessions are live); rebase onto `origin/main` before merging.

**Goal:** Converge all 11 cockpit tabs on the shared `_ui` macro library and make dense tables scroll on mobile — Tailwind-idiomatic, template-only, observe-only.

**Architecture:** Add a `responsive_table()` macro + badge a11y to `components/_ui.html`; apply the wrapper to the 4 dense-table tabs; migrate the 2 non-`_ui` tabs (`portfolio_config`, `portfolio_sync`) onto the macros. No `:root` tokens / no raw `@media` (Tailwind handles responsive). No data-loader/route/behavior change.

**Tech Stack:** Jinja2 templates, Tailwind CSS (CDN), htmx; FastAPI `TestClient` for smoke tests. Run Python via `.venv/bin/python3`.

**Conventions:** Template/markup ONLY — never touch `gui_v2/data/*.py`, routes, or loader behavior. Observe-only banner stays on every tab. Preserve the `portfolio_config` gated edit form exactly. TDD where testable (smoke + grep-style asserts). Commit per task. Stage explicit paths.

---

## File Structure

- **Modify** `gui_v2/templates/components/_ui.html` — add `responsive_table()` macro + a11y on `status_badge`/`badge` (Task 1).
- **Modify** `gui_v2/templates/dashboard/{portfolio,system,portfolio_sync,strategy_tax}.html` — wrap dense tables (Task 2).
- **Modify** `gui_v2/templates/dashboard/portfolio_sync.html` — migrate onto `_ui` (Task 3).
- **Modify** `gui_v2/templates/dashboard/portfolio_config.html` — migrate onto `_ui`, preserve edit form (Task 4).
- **Create** `tests/test_gui_polish.py` (Tasks 1–4 add to it).

---

## Task 1: `_ui` responsive_table macro + badge a11y

**Files:** Modify `gui_v2/templates/components/_ui.html`; Test `tests/test_gui_polish.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_gui_polish.py`:

```python
from pathlib import Path

_UI = Path("gui_v2/templates/components/_ui.html")


def test_ui_has_responsive_table_macro():
    src = _UI.read_text(encoding="utf-8")
    assert "macro responsive_table" in src
    assert "overflow-x-auto" in src


def test_ui_badges_have_a11y():
    src = _UI.read_text(encoding="utf-8")
    # both badge macros expose role/aria-label for screen readers
    assert src.count('role="status"') >= 1
    assert "aria-label" in src
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python3 -m pytest -q tests/test_gui_polish.py`
Expected: FAIL (no `responsive_table` macro; badges lack `aria-label`).

- [ ] **Step 3: Implement**

In `gui_v2/templates/components/_ui.html`, append this macro at the end of the file:

```jinja
{# Responsive table wrapper — horizontal scroll on narrow screens (mobile).
   Usage:  {% call ui.responsive_table() %}<table>...</table>{% endcall %} #}
{% macro responsive_table() -%}
<div class="overflow-x-auto -mx-2 sm:mx-0">
  <div class="inline-block min-w-full align-middle px-2 sm:px-0">
    {{ caller() }}
  </div>
</div>
{%- endmacro %}
```

Then update the two badge macros (currently `status_badge` and `badge`) to add a11y attributes WITHOUT changing their classes:

`status_badge` →
```jinja
{% macro status_badge(severity, label) -%}
<span role="status" aria-label="{{ (label or severity) | status_label }}" class="inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium border {{ _sev_classes(severity) }}">{{ (label or severity) | status_label }}</span>
{%- endmacro %}
```

`badge` →
```jinja
{% macro badge(severity, label) -%}
<span role="status" aria-label="{{ label }}" class="inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium border {{ _sev_classes(severity) }}">{{ label }}</span>
{%- endmacro %}
```

- [ ] **Step 4: Run to verify it passes**

Run: `.venv/bin/python3 -m pytest -q tests/test_gui_polish.py`
Expected: PASS (2 tests).

- [ ] **Step 5: Smoke-test a couple tabs that use these macros render fine**

Run: `.venv/bin/python3 -c "from gui_v2.app import app; from fastapi.testclient import TestClient; c=TestClient(app); print('quant', c.get('/dashboard/quant').status_code, 'today', c.get('/dashboard/today').status_code)"`
Expected: both `200` (or 401 if auth env set) — not 500.

- [ ] **Step 6: Commit**

```bash
git add gui_v2/templates/components/_ui.html tests/test_gui_polish.py
git commit -m "feat(gui-polish): responsive_table macro + badge a11y in _ui"
```

---

## Task 2: Wrap dense tables on the 4 table-heavy tabs

**Files:** Modify `gui_v2/templates/dashboard/{portfolio,system,portfolio_sync,strategy_tax}.html`; Test `tests/test_gui_polish.py`

- [ ] **Step 1: Add the failing test**

Append to `tests/test_gui_polish.py`:

```python
import pytest

_DENSE = ["portfolio", "system", "portfolio_sync", "strategy_tax"]


@pytest.mark.parametrize("tab", _DENSE)
def test_dense_tabs_wrap_tables(tab):
    src = Path(f"gui_v2/templates/dashboard/{tab}.html").read_text(encoding="utf-8")
    # each dense tab routes its table(s) through the responsive wrapper
    assert "responsive_table" in src or "overflow-x-auto" in src
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python3 -m pytest -q tests/test_gui_polish.py -k dense`
Expected: FAIL for tabs that don't yet wrap tables.

- [ ] **Step 3: Implement — for EACH of the 4 templates**

For each file, ensure it imports `_ui` at the top (add `{% import "components/_ui.html" as ui %}` after the `{% extends %}` line if not already imported — `portfolio`, `system`, `strategy_tax` already import it; `portfolio_sync` gets it in Task 3, so for Task 2 wrap its table with a raw `<div class="overflow-x-auto -mx-2 sm:mx-0">...</div>` to satisfy the test without depending on the import). READ each file, find each `<table ...>...</table>` block, and wrap it:

```jinja
{% call ui.responsive_table() %}
<table ...>
  ...existing table unchanged...
</table>
{% endcall %}
```

`portfolio.html` has TWO `<table>` blocks — wrap BOTH. `system.html`, `strategy_tax.html` have one each. For `portfolio_sync.html` (no `_ui` import yet) wrap its one table in a plain `<div class="overflow-x-auto -mx-2 sm:mx-0"> ... </div>` (Task 3 will switch it to the macro during migration).

Do NOT change any table content, classes, htmx attributes, or data bindings — only add the wrapper around the `<table>` element.

- [ ] **Step 4: Run to verify it passes**

Run: `.venv/bin/python3 -m pytest -q tests/test_gui_polish.py -k dense`
Expected: PASS (4).

- [ ] **Step 5: Smoke-test all 4 tabs render**

Run:
```bash
.venv/bin/python3 -c "
from gui_v2.app import app; from fastapi.testclient import TestClient
c=TestClient(app)
for p in ['portfolio','system','portfolio-sync','strategy-tax']:
    print(p, c.get('/dashboard/'+p).status_code)
"
```
Expected: each `200` (or 401) — not 500.

- [ ] **Step 6: Commit**

```bash
git add gui_v2/templates/dashboard/portfolio.html gui_v2/templates/dashboard/system.html gui_v2/templates/dashboard/portfolio_sync.html gui_v2/templates/dashboard/strategy_tax.html tests/test_gui_polish.py
git commit -m "feat(gui-polish): wrap dense tables for mobile horizontal scroll"
```

---

## Task 3: Migrate `portfolio_sync.html` onto `_ui`

**Files:** Modify `gui_v2/templates/dashboard/portfolio_sync.html`; Test `tests/test_gui_polish.py`

- [ ] **Step 1: Add the failing test**

Append to `tests/test_gui_polish.py`:

```python
def test_portfolio_sync_imports_ui():
    src = Path("gui_v2/templates/dashboard/portfolio_sync.html").read_text(encoding="utf-8")
    assert 'import "components/_ui.html"' in src
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python3 -m pytest -q tests/test_gui_polish.py -k portfolio_sync_imports`
Expected: FAIL.

- [ ] **Step 3: Implement**

READ `gui_v2/templates/dashboard/portfolio_sync.html` fully and READ a reference tab that already uses `_ui` well (e.g. `gui_v2/templates/dashboard/system.html` or `quant.html`) to match the pattern. Then:
1. Add `{% import "components/_ui.html" as ui %}` immediately after the `{% extends "base.html" %}` line.
2. Replace the hand-rolled `<header>...</header>` block with the shared `ui.page_header(...)` macro (read its signature in `_ui.html` — `page_header(title, subtitle, route, target_id)` with an optional `{% call %}` for extra buttons). Keep the existing title/subtitle text and the htmx refresh wiring (the macro's button already does `hx-get`/`hx-target`/`hx-select` — point it at `/dashboard/portfolio-sync` and the content container id `dashboard-portfolio-sync-content`).
3. Convert any inline status pills / severity dots to `ui.status_badge(...)` / `ui.sev_dot(...)`.
4. Switch the table wrapper added in Task 2 from the raw `<div class="overflow-x-auto...">` to `{% call ui.responsive_table() %}...{% endcall %}`.
5. PRESERVE every data field, the `hx-get every 120s` auto-refresh on the content container, and all observe-only/read-only text. No data binding changes.

- [ ] **Step 4: Run to verify it passes + renders**

Run: `.venv/bin/python3 -m pytest -q tests/test_gui_polish.py -k portfolio_sync`
Expected: PASS.
Run: `.venv/bin/python3 -c "from gui_v2.app import app; from fastapi.testclient import TestClient; print(TestClient(app).get('/dashboard/portfolio-sync').status_code)"`
Expected: `200` (or 401) — not 500.

- [ ] **Step 5: Commit**

```bash
git add gui_v2/templates/dashboard/portfolio_sync.html tests/test_gui_polish.py
git commit -m "feat(gui-polish): migrate portfolio_sync onto shared _ui macros"
```

---

## Task 4: Migrate `portfolio_config.html` onto `_ui` (preserve edit form)

**Files:** Modify `gui_v2/templates/dashboard/portfolio_config.html`; Test `tests/test_gui_polish.py`

- [ ] **Step 1: Add the failing test**

Append to `tests/test_gui_polish.py`:

```python
def test_portfolio_config_imports_ui_and_keeps_edit_form():
    src = Path("gui_v2/templates/dashboard/portfolio_config.html").read_text(encoding="utf-8")
    assert 'import "components/_ui.html"' in src
    # the gated edit form include MUST be preserved
    assert 'include "components/portfolio_edit_form.html"' in src
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python3 -m pytest -q tests/test_gui_polish.py -k portfolio_config`
Expected: FAIL (no `_ui` import).

- [ ] **Step 3: Implement**

READ `gui_v2/templates/dashboard/portfolio_config.html` fully. Then:
1. Add `{% import "components/_ui.html" as ui %}` after `{% extends "base.html" %}`.
2. Replace the hand-rolled `<header>` with `ui.page_header("Portfolio Config", "Local holdings & cash configuration — advisory system only", "/dashboard/portfolio-config", "portfolio-config-content")`.
3. Convert the inline sky safety-banner `<div>` and any inline status pills to the shared macros where a clean equivalent exists (e.g. `ui.badge(...)`); KEEP the safety-banner wording (no forbidden action labels).
4. **DO NOT TOUCH** the `{% include "components/portfolio_edit_form.html" %}` (line ~94) or anything about the edit form's gating, POST action, or field names. Only the surrounding presentation changes.
5. PRESERVE the `#portfolio-config-content` container id (the refresh button targets it).

- [ ] **Step 4: Run to verify it passes + renders (both gated states)**

Run: `.venv/bin/python3 -m pytest -q tests/test_gui_polish.py -k portfolio_config`
Expected: PASS.
Run: `.venv/bin/python3 -c "from gui_v2.app import app; from fastapi.testclient import TestClient; print(TestClient(app).get('/dashboard/portfolio-config').status_code)"`
Expected: `200` (or 401) — not 500.

- [ ] **Step 5: Commit**

```bash
git add gui_v2/templates/dashboard/portfolio_config.html tests/test_gui_polish.py
git commit -m "feat(gui-polish): migrate portfolio_config onto shared _ui macros (edit form preserved)"
```

---

## Task 5: Full smoke + no-loader-change guard + finalize

**Files:** none (verification)

- [ ] **Step 1: All routes smoke-test**

Run:
```bash
.venv/bin/python3 -c "
from gui_v2.app import app; from fastapi.testclient import TestClient
c=TestClient(app)
tabs=['today','portfolio','quant','strategy-lab','crowd-radar','strategy-tax','system','memo','portfolio-sync','portfolio-config']
bad=[(t,c.get('/dashboard/'+t).status_code) for t in tabs if c.get('/dashboard/'+t).status_code not in (200,401)]
print('FAIL:' , bad if bad else 'none — all 200/401')
"
```
Expected: `none — all 200/401`.

- [ ] **Step 2: Confirm NO loader/route/behavior change**

Run: `git diff --stat origin/main..HEAD -- gui_v2/data/ gui_v2/app.py`
Expected: EMPTY (this feature touches templates + tests only; `app.py` and `gui_v2/data/*` unchanged).

- [ ] **Step 3: Full gui suite + targeted polish suite**

Run: `.venv/bin/python3 -m pytest -q tests/test_gui_polish.py -k "" ; .venv/bin/python3 -m pytest -q -k gui`
Expected: all PASS (polish file + the broader gui suite).

- [ ] **Step 4: Full suite**

Run: `.venv/bin/python3 -m pytest -q`
Expected: PASS except the 3 known pre-existing failures. No NEW failures.

- [ ] **Step 5: Finalize the worktree (controller)**

Rebase the worktree branch onto `origin/main` (pick up concurrent `gui_v2` commits), resolve any conflict in the touched templates, then fast-forward `main` and push. Remove the worktree. (Controller-run; see the execution handoff.)

- [ ] **Step 6: Operator note**

Dashboard must be restarted to serve the changes: `sudo systemctl restart stockbot-dashboard.service`. Then eyeball the tabs on a phone (the visual-quality check the tests can't do).

---

## Notes for the implementer

- Run all Python via `.venv/bin/python3`.
- Template/markup ONLY — never edit `gui_v2/data/*.py`, routes, or loader logic. If a change seems to require a loader edit, STOP and report.
- Preserve the `portfolio_config` gated edit form (include, POST action, field names, gating) byte-for-byte except surrounding presentation.
- No `:root` token CSS, no raw `@media` — this is a Tailwind app; use Tailwind utilities (`overflow-x-auto`, `sm:`/`md:` prefixes).
- 401 on a smoke-test is acceptable (means `GUI_V2_AUTH_*` is set in the env) — only 500 is a failure.
