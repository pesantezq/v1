# Supervisor Evidence Screen — trust boundary

Two different questions were being answered by one detector. Separating them is
the whole change.

| Boundary | Question | Guard | Status |
|---|---|---|---|
| Production evidence admission | May this runtime artifact be admitted as evidence? | `prod_evidence._detect_secret` | **UNCHANGED** |
| Supervisor evidence transfer | May this evidence be sent to the independent reviewer? | `supervisor_screen` (new) | new, more precise |
| Canonical descriptor contract | May this value be persisted in a descriptor? | `northstar/sources._SECRET_PATTERN` | **UNCHANGED** |

## Why this existed

Northstar 0B.1 (Evidence Kernel) certification returned **ABSTAIN**. Not because
the kernel looked defective — because the reviewer could not see it.

`gpt_supervisor._screen_packet` applied the production free-text detector to the
supervisor packet. That detector's credential-assignment rule (keyword, then `=`
or `:`, then any 4+ non-space characters) is correct for a production log, where
`token=<anything>` really is a leak. Applied to **source code**, it fires on:

* `portfolio_automation/northstar/sources.py:49` — the Evidence Plane's *own*
  credential guard, a `re.compile(...)` pattern naming `authorization:`,
  `token=`, `sk-…`. A pattern definition, not a value.
* `tests/test_northstar_evidence_kernel.py` — synthetic fixtures whose entire
  purpose is proving that guard rejects them.

So the files that *are* the security evidence were the files that could not be
transmitted. The reviewer correctly refused to certify what it could not read.

## Design

Three layers. Any layer blocks; only layers 2 and 3 exempt.

### Layer 1 — high-confidence shapes (context-free, never exemptible)

Private-key blocks, provider keys (`sk-…{16,}`), AWS (`AKIA…`), GitHub
(`gh[pousr]_…{36,}`), JWTs, `Bearer`/`Authorization` values of plausible length.

Applied to **all** evidence regardless of context. **This is what makes layer 2
safe to relax**: even if the structural classifier were fooled, a real credential
*shape* still cannot pass. `re.compile('sk-<realkey>')` is blocked here.

### Layer 2 — credential assignment, context-aware

Same keyword family as the production detector. A match is exempt **only** if it
lies wholly inside a string literal that the **AST** proves is a direct argument
to an `re.*` call.

Deliberate properties:

* **Structural, not name-based.** `_SECRET_PATTERN = "sk-realkey"` is a plain
  assignment, yields no span, and is blocked. A convenient variable name buys
  nothing.
* **No leading `\b`.** `fake_api_key = "..."`, `TEST_token = "..."` and
  `dummy_password = "..."` are all blocked. The production detector's word
  boundary lets those through; this screen deliberately closes that hole — a
  strict improvement, not a relaxation.
* **Quote-aware value capture.** `config["token"] = "..."` is caught. The value
  excludes quotes, which is also load-bearing: a greedy `\S+` runs past the
  closing quote of its own string literal and would silently defeat the AST span
  comparison.
* **Non-Python evidence gets no structural parse**, therefore no exemption.
  Diffs, logs and prose fail closed.
* **Unparseable source yields no spans** — fail closed.

### Layer 3 — synthetic sentinel

Exactly one representation is recognised:

```
<synthetic-secret-fixture>
```

The value must **equal** it. Not prefix, not suffix, not substring — so
`api_key=<synthetic-secret-fixture>REALKEY` and
`api_key=REALKEY<synthetic-secret-fixture>` are both blocked.

There is deliberately **no** `TEST_` / `fake_` / `example_` / `dummy_` prefix
exemption. That would be an obvious exfiltration escape hatch.

## What the sentinel does NOT do

It is a **transfer** mechanism, never a contract exemption.
`DataSourceDescriptor` still rejects `api_key=<synthetic-secret-fixture>`,
because credential material has no place in a persisted, hashed descriptor
regardless of how the value is written. Pinned by
`test_descriptor_rejects_sentinel_bearing_material`.

`prod_evidence._detect_secret` also still flags it. That boundary is untouched.

## Rejected design

An earlier proposal exempted a value containing regex metacharacters
(`| [ ] { } ( )`). **Rejected in security review as unsafe**, correctly: a real
credential may contain those characters. `password=Strong(Password)Value` is a
plausible secret, and a content-only rule would have waved it through. The
adversarial suite pins that class permanently:

```
api_key=RealSecret|MoreSecret        blocked
token=abc[123]Secret                 blocked
password=Strong(Password)Value       blocked
client_secret=abc?def*ghi            blocked
Authorization: Bearer Real|Secret    blocked
```

## Findings carry no secrets

A `Finding` records the rule, layer, location and line — **never the matched
value**. Reporting it would write the credential into logs, which is the harm
being prevented.

## Testing

`tests/test_ew0a_supervisor_screen.py` (50 tests):

* **A. must block** — high-confidence shapes, credential assignments, nested
  packet structures, secrets in source/test/prose
* **B. must allow** — the real `sources.py`, the exact regex line that caused the
  ABSTAIN, detector regex source, redacted placeholders, sentinel fixtures, docs
* **C. bypass** — sentinel prefix/suffix/substring, fake/test prefixes,
  convenient variable names, laundering through `re.compile`, casing, whitespace,
  quoting, unparseable source, malformed fields
* **D. boundary** — production detector and descriptor guard unchanged, plus a
  false-negative regression guard asserting the new screen blocks everything the
  production detector blocks on flat text

### Fixture construction

Credential-shaped fixtures are assembled at runtime rather than stored as
literals. A repository secret scanner is *correct* to flag a literal token shape,
and this suite necessarily needs such shapes to prove layer 1 works. Building
them from parts keeps the scanner honest **without suppressing it** — no
`nosemgrep`. Same principle as the screen itself: precision, not suppression.
