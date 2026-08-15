"""Supervisor evidence secret screen — precision for source-code review.

A SEPARATE trust boundary from ``prod_evidence._detect_secret``, which guards
production runtime evidence admission and is deliberately left untouched. That
detector is correct for its own boundary: in a production log, a token assignment
carrying any value really is a credential leak, and there is no source structure
to reason about.

(Note on style: this module documents credential patterns by DESCRIBING them
rather than by exhibiting literal keyword-equals-value examples. Docstrings get
copied into tickets, logs and review packets, so a literal example would be a
credential-shaped string in transit — and would make this very file
untransmittable to the independent reviewer.)

This screen guards a different question: may this evidence be transmitted to the
INDEPENDENT supervisor for review? Source code legitimately *names* credential
keywords inside pattern definitions, and security tests legitimately carry
synthetic credential-shaped fixtures whose whole purpose is to prove they are
rejected. Screening those with a flat free-text rule starves the reviewer of the
security evidence it needs — which is exactly how the Northstar 0B.1 evidence
kernel certification came back ABSTAIN.

Three layers, in order. Any layer may block; only layers 2 and 3 can exempt.

  Layer 1  HIGH-CONFIDENCE SHAPES — context-free, applied to ALL evidence,
           NEVER exemptible. Private keys, provider keys, AWS keys, GitHub
           tokens, JWTs, Bearer values. This is what makes any relaxation in
           layer 2 safe: even if the structural classifier were fooled, a real
           credential SHAPE still cannot pass.

  Layer 2  CREDENTIAL ASSIGNMENT — the same keyword rule as the production
           detector, but context-aware for Python source. A match is exempt ONLY
           if it lies wholly inside a string literal that is a direct argument to
           an ``re.*`` call, determined by AST — never by variable name, so
           assigning a real secret to a conveniently named ``_SECRET_PATTERN``
           buys nothing. Non-Python evidence gets no structural parse and
           therefore NO exemption.

  Layer 3  SYNTHETIC SENTINEL — a value EXACTLY equal to the sentinel constant
           below. Equality only: a value consisting of the sentinel followed by
           (or preceded by) further characters is NOT exempt, so appending a real
           key to the sentinel stays blocked. There is deliberately no
           ``TEST_``/``fake_``/``dummy_`` exemption, which would be an obvious
           exfiltration escape hatch.

Fail-closed everywhere: an unparseable source yields no exempt spans (strict), and
any error yields a finding rather than silence.

``experimental_noncanonical``.
"""
from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field
from typing import Any, Iterable

SCREEN_SCHEMA_VERSION = "engineering.supervisor_screen.v0"

# The ONLY synthetic-fixture representation this screen recognizes. It must be
# matched EXACTLY as the assigned value.
SYNTHETIC_SECRET_SENTINEL = "<synthetic-secret-fixture>"

# ---------------------------------------------------------------------------
# Layer 1 — high-confidence secret shapes (context-free, never exemptible)
# ---------------------------------------------------------------------------
_HIGH_CONFIDENCE: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("private_key_block",
     re.compile(r"-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----")),
    ("provider_api_key",
     re.compile(r"(?<![A-Za-z0-9])sk-[A-Za-z0-9_\-]{16,}")),
    ("aws_access_key_id",
     re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("github_token",
     re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b")),
    ("jwt",
     re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}")),
    # A Bearer/Authorization value long enough to be a real token. Short tokens
    # like "Bearer xyz" are implausible as credentials and are left to layer 2.
    ("bearer_value",
     re.compile(r"(?i)\b(?:authorization\s*:\s*)?bearer[ \t]+[A-Za-z0-9._\-]{8,}")),
)

# ---------------------------------------------------------------------------
# Layer 2 — credential assignment (same keywords as the production detector)
# ---------------------------------------------------------------------------
_CREDENTIAL_KEYWORDS = (
    r"password|secret|token|api[_-]?key|apikey|authorization|bearer|"
    r"refresh_token|access_token|client_secret|schwab[_-]?token")

# Placeholders that are definitionally not credentials. Compared against the
# CAPTURED value rather than applied as a lookahead, so a quoted "<redacted>" is
# recognised as a placeholder instead of as a value starting with a quote.
_PLACEHOLDERS = frozenset({"<redacted>", "redacted", "null", "none", "nil",
                           "changeme", "xxxx", "...."})

# Deliberate construction:
#
# * NO leading \b. ``fake_api_key = "..."`` and ``TEST_token = "..."`` ARE
#   credential assignments; a leading word boundary would let any identifier
#   prefix bypass the rule — exactly the fake_/TEST_/dummy_ escape hatch the
#   security review forbids. The production detector has that hole; this screen
#   deliberately closes it, which is a strict improvement, not a relaxation.
# * An optional quote/bracket run between keyword and operator, so
#   ``config["token"] = "..."`` is caught as the value-bearing construct it is.
# * The value excludes whitespace AND quotes. This is both more accurate and
#   load-bearing for layer 2: a greedy \S+ runs PAST the closing quote of its
#   enclosing string literal, which would defeat the AST span comparison and
#   silently disable the regex-literal exemption.
_CREDENTIAL_ASSIGNMENT = re.compile(
    rf"(?:{_CREDENTIAL_KEYWORDS})"
    r"[\"'\]\[]{0,2}"
    r"\s*[:=]\s*"
    r"[\"']?"
    r"(?P<value>[^\s\"']{4,})",
    re.IGNORECASE)

# Trailing syntax that belongs to the surrounding code, not to the value.
_TRAILING = "\"',;)]}\\"

# re.* functions whose first string argument is a regex PATTERN, not a value.
_RE_FUNCS = frozenset({"compile", "match", "search", "fullmatch", "sub", "subn",
                       "split", "findall", "finditer"})


class ScreenError(ValueError):
    """Deterministic, fail-closed screening error."""


@dataclass(frozen=True)
class Finding:
    """A blocked item. NEVER carries the matched secret value — reporting it
    would leak the credential into logs, which is the harm being prevented."""
    rule: str
    layer: int
    where: str
    line: int | None = None
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"rule": self.rule, "layer": self.layer, "where": self.where,
                "line": self.line, "detail": self.detail}


@dataclass
class ScreenResult:
    findings: list[Finding] = field(default_factory=list)
    exempted: list[str] = field(default_factory=list)

    @property
    def blocked(self) -> bool:
        return bool(self.findings)

    def to_dict(self) -> dict[str, Any]:
        return {"schema_version": SCREEN_SCHEMA_VERSION, "blocked": self.blocked,
                "findings": [f.to_dict() for f in self.findings],
                "exempted": self.exempted}


def _line_of(text: str, pos: int) -> int:
    return text.count("\n", 0, pos) + 1


def _offsets(source: str) -> list[int]:
    """Absolute character offset of the start of each 1-indexed line."""
    offs, total = [0, 0], 0
    for line in source.splitlines(keepends=True):
        total += len(line)
        offs.append(total)
    return offs


def regex_literal_spans(source: str) -> list[tuple[int, int]]:
    """Absolute character spans of string literals that are DIRECT arguments to
    an ``re.*`` call.

    Structural by construction: the decision comes from the call being made, not
    from the name the result is bound to. ``_SECRET_PATTERN = "sk-realkey..."``
    is a plain assignment and yields no span, so a convenient variable name
    cannot launder a real credential.

    Returns [] for unparseable source (fail closed → nothing is exempt)."""
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return []
    offs = _offsets(source)
    spans: list[tuple[int, int]] = []

    def _abs(node: ast.AST) -> tuple[int, int] | None:
        lineno = getattr(node, "lineno", None)
        end_lineno = getattr(node, "end_lineno", None)
        if lineno is None or end_lineno is None:
            return None
        try:
            return (offs[lineno] + node.col_offset,
                    offs[end_lineno] + node.end_col_offset)
        except IndexError:
            return None

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        is_re_call = (isinstance(fn, ast.Attribute) and fn.attr in _RE_FUNCS
                      and isinstance(fn.value, ast.Name) and fn.value.id == "re")
        if not is_re_call:
            continue
        for arg in node.args:
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                span = _abs(arg)
                if span:
                    spans.append(span)
    return spans


def _within(span: tuple[int, int], spans: Iterable[tuple[int, int]]) -> bool:
    return any(s <= span[0] and span[1] <= e for s, e in spans)


def _is_sentinel(raw_value: str) -> bool:
    """EXACT sentinel match after stripping trailing code syntax.

    Exactness is the whole safety property: ``<synthetic-secret-fixture>REALKEY``
    and ``REALKEY<synthetic-secret-fixture>`` both fail this test."""
    return raw_value.rstrip(_TRAILING) == SYNTHETIC_SECRET_SENTINEL


def high_confidence_findings(text: str, where: str) -> list[Finding]:
    """Layer 1. Applied to every piece of evidence, exemptible by nothing."""
    out: list[Finding] = []
    for rule, rx in _HIGH_CONFIDENCE:
        for m in rx.finditer(text):
            out.append(Finding(rule=rule, layer=1, where=where,
                               line=_line_of(text, m.start()),
                               detail="high-confidence secret shape (never exemptible)"))
    return out


def credential_assignment_findings(text: str, where: str, *,
                                   exempt_spans: Iterable[tuple[int, int]] = (),
                                   result: ScreenResult | None = None) -> list[Finding]:
    """Layers 2 and 3."""
    spans = list(exempt_spans)
    out: list[Finding] = []
    for m in _CREDENTIAL_ASSIGNMENT.finditer(text):
        value = m.group("value")
        line = _line_of(text, m.start())

        if value.rstrip(_TRAILING).lower() in _PLACEHOLDERS:
            continue

        if _is_sentinel(value):
            if result is not None:
                result.exempted.append(f"{where}:{line} synthetic sentinel")
            continue

        if _within((m.start(), m.end()), spans):
            if result is not None:
                result.exempted.append(f"{where}:{line} regex pattern literal (AST)")
            continue

        out.append(Finding(rule="credential_assignment", layer=2, where=where, line=line,
                           detail="credential keyword assigned a value outside a regex "
                                  "pattern literal"))
    return out


def screen_source(path: str, content: str) -> ScreenResult:
    """Screen ONE Python source file with structural context."""
    result = ScreenResult()
    result.findings.extend(high_confidence_findings(content, path))
    spans = regex_literal_spans(content) if path.endswith(".py") else []
    result.findings.extend(credential_assignment_findings(
        content, path, exempt_spans=spans, result=result))
    return result


def screen_text(text: str, where: str = "text") -> ScreenResult:
    """Screen non-source evidence. NO structural exemption is available here, so
    the strict flat rule applies — diffs, logs and prose fail closed."""
    result = ScreenResult()
    result.findings.extend(high_confidence_findings(text, where))
    result.findings.extend(credential_assignment_findings(text, where, result=result))
    return result


def _walk_values(obj: Any, prefix: str = "") -> Iterable[tuple[str, str]]:
    """Yield every string in a nested packet, so a secret cannot hide in a list
    or a deeply nested dict."""
    if isinstance(obj, str):
        yield prefix or "value", obj
    elif isinstance(obj, dict):
        for k, v in obj.items():
            yield from _walk_values(v, f"{prefix}.{k}" if prefix else str(k))
    elif isinstance(obj, (list, tuple)):
        for i, v in enumerate(obj):
            yield from _walk_values(v, f"{prefix}[{i}]")


def screen_packet(packet: dict[str, Any]) -> ScreenResult:
    """Screen a full supervisor packet.

    ``source_files``: [{"path": ..., "content": ...}] receives Python-aware
    structural screening. EVERY other field — including nested structures — is
    screened with the strict flat rule."""
    result = ScreenResult()
    source_files = packet.get("source_files") or []

    if not isinstance(source_files, list):
        result.findings.append(Finding(rule="malformed_source_files", layer=0,
                                       where="source_files",
                                       detail="source_files must be a list"))
        source_files = []

    for entry in source_files:
        if not isinstance(entry, dict):
            result.findings.append(Finding(rule="malformed_source_entry", layer=0,
                                           where="source_files",
                                           detail="each entry must be a dict"))
            continue
        path = str(entry.get("path", "unknown"))
        content = entry.get("content")
        if not isinstance(content, str):
            result.findings.append(Finding(rule="malformed_source_content", layer=0,
                                           where=path,
                                           detail="content must be a string"))
            continue
        sub = screen_source(path, content)
        result.findings.extend(sub.findings)
        result.exempted.extend(sub.exempted)

    rest = {k: v for k, v in packet.items() if k != "source_files"}
    for where, text in _walk_values(rest):
        result.findings.extend(high_confidence_findings(text, where))
        result.findings.extend(
            credential_assignment_findings(text, where, result=result))
    return result
