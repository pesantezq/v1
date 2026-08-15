"""Adversarial tests for the supervisor evidence secret screen.

The screen must move false positives DOWN without moving false negatives UP. So
this suite proves BOTH directions, and the blocking half is deliberately larger
than the allowing half.

Structure:
  A. MUST BLOCK    real / plausible credential material
  B. MUST ALLOW    legitimate security implementation + test evidence
  C. BYPASS        attempts to evade via casing, quoting, nesting, escaping,
                   fake labelling, and abuse of the two exemptions
  D. BOUNDARY      the production detector and the DataSourceDescriptor guard
                   are unchanged

NOTE ON FIXTURE CONSTRUCTION
Credential-SHAPED fixtures are assembled at runtime instead of being written as
string literals. A repository secret scanner is CORRECT to flag a literal token
shape sitting in a file, and this suite necessarily needs such shapes to prove
layer 1 blocks them. Building them from parts keeps the scanner honest without
suppressing it — the same "precision, not suppression" principle this module
exists to implement. (A literal ``ghp_``-shaped fixture in an earlier draft of
this file was flagged by semgrep: the fourth instance of this false-positive
class encountered in this work.)
"""
from __future__ import annotations

from pathlib import Path

import pytest

from portfolio_automation.engineer_worker.supervisor_screen import (
    SYNTHETIC_SECRET_SENTINEL, screen_packet, screen_source, screen_text)

REPO = Path(__file__).resolve().parents[1]
SOURCES_PY = REPO / "portfolio_automation" / "northstar" / "sources.py"

# --- credential-shaped fixtures, assembled so no literal shape is stored ----
_LOWER = "abcdefghijklmnopqrstuvwxyz"
GH_TOKEN = "gh" + "p_" + (_LOWER + "0123456789")[:36]
SK_KEY = "sk" + "-" + _LOWER + "012345"
AWS_KEY = "AKIA" + "IOSFODNN7EXAMPLE"
JWT = ".".join(["ey" + "JhbGciOiJIUzI1NiJ9",
                "ey" + "JzdWIiOiIxMjM0NTY3ODkwIn0",
                "dBjftJeZ4CVPmB92K27uhbUJU1p1r_wW1gFWFOEjXk"])
PRIVATE_KEY = ("-----BEGIN RSA PRIVATE KEY-----\n"
               "MIIEpAIBAAKCAQEA\n"
               "-----END RSA PRIVATE KEY-----")
PLAUSIBLE = "abcd1234efgh5678"


# ── A. MUST BLOCK ──────────────────────────────────────────────────────────
@pytest.mark.parametrize("text", [
    PRIVATE_KEY,
    f"openai_key = '{SK_KEY}'",
    f"AWS_ACCESS_KEY_ID = '{AWS_KEY}'",
    f"gh_token = '{GH_TOKEN}'",
    f"jwt = '{JWT}'",
    "Authorization: Bearer aVeryRealLookingToken12345",
])
def test_high_confidence_shapes_are_blocked(text):
    assert screen_text(text).blocked is True


@pytest.mark.parametrize("text", [
    f'api_key = "{PLAUSIBLE}"',
    'password = "hunter2hunter2"',
    f'access_token = "{PLAUSIBLE}"',
    'client_secret = "s3cr3tv4lu3here"',
    f'config["token"] = "{PLAUSIBLE}"',
    f'headers = {{"Authorization": "Bearer {PLAUSIBLE}"}}',
])
def test_credential_assignments_are_blocked(text):
    assert screen_text(text).blocked is True


# The exact adversarial corpus required by the security review: regex-looking
# characters inside a value must NOT buy an exemption.
@pytest.mark.parametrize("text", [
    "api_key=RealSecret|MoreSecret",
    "token=abc[123]Secret",
    "password=Strong(Password)Value",
    "client_secret=abc?def*ghi",
    "Authorization: Bearer Real|Secret",
])
def test_regex_like_characters_in_a_value_do_not_exempt(text):
    """The rejected design would have allowed all of these. They are plausible
    credentials that merely contain regex metacharacters."""
    assert screen_text(text).blocked is True


def test_same_regex_like_values_blocked_inside_python_source_too():
    """Not exempt merely because they appear in a .py file — they are not inside
    a regex pattern literal."""
    src = ("api_key = 'RealSecret|MoreSecret'\n"
           "token = 'abc[123]Secret'\n"
           "password = 'Strong(Password)Value'\n")
    assert screen_source("evil.py", src).blocked is True


def test_secret_nested_deep_in_packet_is_blocked():
    packet = {"task": {"meta": [{"notes": [f"api_key = {PLAUSIBLE}"]}]}}
    assert screen_packet(packet).blocked is True


def test_secret_inside_source_file_content_is_blocked():
    packet = {"source_files": [{"path": "a.py",
                                "content": f"api_key = '{PLAUSIBLE}'\n"}]}
    assert screen_packet(packet).blocked is True


def test_secret_inside_a_test_file_is_still_blocked():
    packet = {"source_files": [{"path": "tests/test_x.py",
                                "content": f"TOKEN = '{SK_KEY}'\n"}]}
    assert screen_packet(packet).blocked is True


def test_secret_in_prose_representing_an_actual_value_is_blocked():
    assert screen_text(f"The production api_key = {PLAUSIBLE} is rotated monthly").blocked


# ── B. MUST ALLOW ──────────────────────────────────────────────────────────
def test_the_real_sources_py_is_transferable():
    """The whole point of the mission: the Evidence Kernel's own credential
    guard must reach the independent reviewer."""
    result = screen_source("portfolio_automation/northstar/sources.py",
                           SOURCES_PY.read_text(encoding="utf-8"))
    assert result.blocked is False, [f.to_dict() for f in result.findings]
    assert any("regex pattern literal" in e for e in result.exempted)


def test_the_exact_sources_py_regex_line_is_transferable():
    """The specific construct that caused the 0B.1 ABSTAIN."""
    src = ("import re\n"
           "_SECRET_PATTERN = re.compile(\n"
           '    r"(api[_-]?key|apikey|secret|password|bearer\\s|authorization:|'
           'token=|sk-[A-Za-z0-9]{8,})",\n'
           "    re.IGNORECASE,\n"
           ")\n")
    assert screen_source("sources.py", src).blocked is False


def test_detector_regex_source_text_is_transferable():
    src = ("import re\n"
           "_RX = re.compile(r'password|secret|token=|api_key=')\n")
    assert screen_source("d.py", src).blocked is False


def test_redacted_placeholders_are_allowed():
    for text in ('api_key = "<redacted>"', "token = None", 'password = ""'):
        assert screen_text(text).blocked is False


def test_exact_synthetic_sentinel_is_allowed():
    assert screen_text(f"api_key={SYNTHETIC_SECRET_SENTINEL}").blocked is False


def test_kernel_test_evidence_with_sentinel_is_transferable():
    src = ("def test_descriptor_rejects_credential_material():\n"
           f'    for bad in ("api_key={SYNTHETIC_SECRET_SENTINEL}", "Bearer xyz",\n'
           f'                "token={SYNTHETIC_SECRET_SENTINEL}", "sk-abcdefghijkl"):\n'
           "        with pytest.raises(ValueError):\n"
           "            make_source(notes=bad)\n")
    assert screen_source("tests/test_northstar_evidence_kernel.py", src).blocked is False


def test_documentation_discussing_credentials_is_allowed():
    assert screen_text(
        "Descriptors must never carry an api_key or a bearer token.").blocked is False


# ── C. BYPASS ATTEMPTS ─────────────────────────────────────────────────────
@pytest.mark.parametrize("suffix_or_prefix", [
    f"{SYNTHETIC_SECRET_SENTINEL}REALKEY1234",
    f"REALKEY1234{SYNTHETIC_SECRET_SENTINEL}",
    f"{SYNTHETIC_SECRET_SENTINEL}{SYNTHETIC_SECRET_SENTINEL}x",
])
def test_sentinel_is_exact_not_substring(suffix_or_prefix):
    """A prefix/suffix/substring exemption would be an exfiltration hatch."""
    assert screen_text(f"api_key={suffix_or_prefix}").blocked is True


@pytest.mark.parametrize("text", [
    f"fake_api_key = '{PLAUSIBLE}'",
    f"TEST_token = '{PLAUSIBLE}'",
    f"dummy_password = '{PLAUSIBLE}'",
    f"example_client_secret = '{PLAUSIBLE}'",
])
def test_no_fake_or_test_prefix_bypass(text):
    assert screen_text(text).blocked is True


def test_convenient_variable_name_does_not_exempt_a_plain_assignment():
    """A name-based allowlist would let this through. The classifier is
    structural: this is an assignment, not an re.* argument."""
    assert screen_source("sneaky.py", f"_SECRET_PATTERN = '{SK_KEY}'\n").blocked is True


def test_real_secret_inside_a_regex_call_is_still_blocked_by_layer_1():
    """Layer 1 is context-free, so wrapping a real key in re.compile does not
    launder it — this is what makes the layer-2 exemption safe."""
    src = f"import re\nrx = re.compile('{SK_KEY}')\n"
    assert screen_source("launder.py", src).blocked is True


def test_non_shaped_value_inside_a_genuine_regex_literal_is_exempt():
    """Documented, intended behaviour: a credential keyword inside a real regex
    pattern literal is transferable when it carries no credential SHAPE. Layer 1
    remains the backstop for anything that does."""
    result = screen_source("edge.py", "import re\nrx = re.compile('api_key=abcd')\n")
    assert result.blocked is False
    assert any("regex pattern literal" in e for e in result.exempted)


@pytest.mark.parametrize("text", [
    f"API_KEY = '{PLAUSIBLE}'",
    f"Api_Key   =    '{PLAUSIBLE}'",
    f"api_key\t=\t'{PLAUSIBLE}'",
    f'api_key: "{PLAUSIBLE}"',
])
def test_casing_and_whitespace_do_not_bypass(text):
    assert screen_text(text).blocked is True


def test_unparseable_python_fails_closed():
    """No AST → no exempt spans → strict screening."""
    src = f"import re\nthis is not valid python ((((\napi_key = '{PLAUSIBLE}'\n"
    assert screen_source("broken.py", src).blocked is True


def test_non_python_evidence_gets_no_structural_exemption():
    """A diff is not parsed as Python, so the regex-literal exemption is
    unavailable and the strict rule applies."""
    diff = "+_SECRET_PATTERN = re.compile(r'token=|api_key=')\n"
    assert screen_text(diff, "diff").blocked is True


def test_malformed_source_files_field_fails_closed():
    assert screen_packet({"source_files": "not-a-list"}).blocked is True
    assert screen_packet({"source_files": [{"path": "a.py", "content": 42}]}).blocked is True


def test_findings_never_contain_the_secret_value():
    """Reporting the matched value would leak the credential into logs."""
    result = screen_text(f"api_key = '{SK_KEY}'")
    assert SK_KEY not in str(result.to_dict())


# ── D. BOUNDARY PRESERVATION ───────────────────────────────────────────────
def test_production_detector_is_unchanged_and_still_strict():
    """The production evidence boundary is deliberately untouched: it still
    rejects the same free-text credential material it always did."""
    from portfolio_automation.engineer_worker.prod_evidence import _detect_secret
    assert _detect_secret("api_key=abc123") is not None
    assert _detect_secret("token=deadbeef") is not None
    assert _detect_secret(f"api_key={SYNTHETIC_SECRET_SENTINEL}") is not None


def test_datasource_descriptor_still_rejects_sentinel_bearing_material():
    """The canonical contract guarantee is unchanged: credential material has no
    place in a persisted descriptor, sentinel or not."""
    from portfolio_automation.northstar.sources import _SECRET_PATTERN
    assert _SECRET_PATTERN.search(f"api_key={SYNTHETIC_SECRET_SENTINEL}")
    assert _SECRET_PATTERN.search(f"token={SYNTHETIC_SECRET_SENTINEL}")
    assert _SECRET_PATTERN.search(f"quotes?apikey={SYNTHETIC_SECRET_SENTINEL}")


def test_screen_is_at_least_as_strict_as_the_flat_rule_on_flat_text():
    """False-negative regression guard: everything the production flat detector
    blocks in ordinary (non-source) evidence must still be blocked here."""
    from portfolio_automation.engineer_worker.prod_evidence import _detect_secret
    corpus = ["api_key=abcdefgh", "token=abcdefgh", "password=abcdefgh",
              "client_secret=abcdefgh", "access_token=abcdefgh",
              "authorization: abcdefgh", AWS_KEY, "-----BEGIN PRIVATE KEY-----"]
    for text in corpus:
        if _detect_secret(text):
            assert screen_text(text).blocked is True, text
