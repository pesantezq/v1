"""
Class-level regression guard: no gui_v2 template may interpolate a Jinja
expression inside an inline event-handler attribute (onclick, onsubmit, etc.).

Rationale: Jinja's ``{{ }}`` autoescaping protects HTML body/attribute
contexts but NOT JavaScript-string context. A value like an apostrophe
autoescapes to ``&#39;`` in an HTML attribute, but browsers HTML-decode
attribute values *before* the JS parser sees them — so ``&#39;`` still
decodes back to ``'`` inside an ``onclick="...">`` handler and can break out
of a single-quoted JS string (e.g. a ``confirm('...')`` call). This was
caught and fixed for one panel in commit d6bb7bcf; this guard protects the
whole vulnerability class, in every template, going forward.

The safe pattern (matching d6bb7bcf): keep the confirm() guard with STATIC
text only; dynamic identifiers stay visible in the surrounding autoescaped
HTML attribute/body context (e.g. hidden `value="{{ ... }}"` inputs), never
inside an `on*=` handler.
"""
from __future__ import annotations

import re
from pathlib import Path

TEMPLATES_DIR = Path(__file__).resolve().parents[1] / "gui_v2" / "templates"

# Matches on<word>="..." or on<word>='...' handler attributes and captures
# the attribute value. Handles both quote styles; does not attempt to
# handle escaped quotes inside the value (none of these templates use them).
_HANDLER_RE = re.compile(
    r"""on[a-z]+\s*=\s*(?:"([^"]*)"|'([^']*)')""",
    re.IGNORECASE,
)


def _iter_template_files():
    return sorted(TEMPLATES_DIR.rglob("*.html"))


def test_templates_directory_is_nonempty():
    # Sanity check so a misconfigured path doesn't silently pass everything.
    files = _iter_template_files()
    assert len(files) >= 10, f"expected many templates under {TEMPLATES_DIR}, found {len(files)}"


def test_no_jinja_expression_inside_inline_event_handlers():
    """No on<word>=\"...\" / on<word>='...' attribute may contain '{{'.

    This is the durable guard against the whole class of JS-context XSS via
    Jinja interpolation inside inline handlers — not just today's 4 known
    offenders. A handler attribute may still exist (e.g. a static-text
    confirm() guard); it just cannot contain a template expression.
    """
    offenses: list[str] = []
    for path in _iter_template_files():
        text = path.read_text(encoding="utf-8")
        lines = text.splitlines()
        for match in _HANDLER_RE.finditer(text):
            value = match.group(1) if match.group(1) is not None else match.group(2)
            if "{{" in value:
                # Locate the line number for a useful failure message.
                line_no = text.count("\n", 0, match.start()) + 1
                line_text = lines[line_no - 1].strip() if line_no - 1 < len(lines) else ""
                rel = path.relative_to(TEMPLATES_DIR.parent.parent)
                offenses.append(f"{rel}:{line_no}: {line_text}")

    assert not offenses, (
        "Found Jinja expression(s) inside inline event-handler attribute(s) — "
        "this is a JS-context XSS vector (autoescape does not protect JS-string "
        "context). Remove the {{ ... }} from inside the on*= handler and keep "
        "only static confirm() text (dynamic values stay in the surrounding "
        "autoescaped HTML). Offending locations:\n" + "\n".join(offenses)
    )
