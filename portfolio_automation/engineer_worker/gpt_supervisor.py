"""Trusted GPT supervisor adapter (EW-0A independent verification).

The INDEPENDENT verifier. Neither the Engineer Worker nor Claude may certify their
own work. This adapter sends a bounded, secret-screened *supervisor packet* (task
+ requirements + acceptance criteria + evidence: changed files, diff, tests run,
verification results, failure classification) to the OpenAI API and returns GPT's
structured verdict: PASS / REPAIR / ESCALATE / ABSTAIN.

Hard credential rules (enforced here):
* The API key is read from a trusted file path (``SupervisorConfig.key_file``),
  ONLY inside the HTTP transport, ONLY to build the Authorization header. It is
  NEVER placed in the packet, the prompt, a result, a log, or the sandbox, and is
  NEVER model-controlled. The key file lives outside the repo and worktrees.
* The packet is secret-screened before transmission, so worker/candidate content
  cannot exfiltrate credentials to OpenAI.
* Failure is FAIL CLOSED: any transport/auth/parse error -> SUPERVISOR_UNAVAILABLE,
  never PASS. Claude is never substituted for GPT.

``experimental_noncanonical``. Does not define canonical Northstar contracts.
"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable

from portfolio_automation.engineer_worker import EXPERIMENTAL_MARKER
from portfolio_automation.engineer_worker import supervisor_screen

SCHEMA_KIND = EXPERIMENTAL_MARKER
SUPERVISOR_SCHEMA_VERSION = "engineering.supervisor_decision.v0"
_MAX_PACKET_BYTES = 256 * 1024          # bound the packet sent to the supervisor
_MAX_RESPONSE_BYTES = 128 * 1024

# Extra screen for provider API-key shapes (belt-and-suspenders atop _detect_secret).
_APIKEY_RE = re.compile(r"\b(?:sk-[A-Za-z0-9_\-]{16,}|AKIA[0-9A-Z]{16})\b")


class SupervisorError(ValueError):
    """Deterministic, fail-closed supervisor error."""


class SupervisorVerdict(str, Enum):
    PASS = "PASS"
    REPAIR = "REPAIR"
    ESCALATE = "ESCALATE"
    ABSTAIN = "ABSTAIN"
    SUPERVISOR_UNAVAILABLE = "SUPERVISOR_UNAVAILABLE"   # link/auth/parse failure — NOT a pass


_DECISION_VERDICTS = frozenset({
    SupervisorVerdict.PASS, SupervisorVerdict.REPAIR,
    SupervisorVerdict.ESCALATE, SupervisorVerdict.ABSTAIN,
})


@dataclass
class SupervisorConfig:
    """Trusted-controller-owned supervisor connection facts. NEVER model-controlled,
    NEVER placed in the sandbox / repo / worktree / prompt."""
    key_file: str | None = None            # 0600 file outside the repo; read only in transport
    model: str = "gpt-4o"
    api_base: str = "https://api.openai.com/v1"
    timeout: int = 60
    max_completion_tokens: int = 2000
    max_packet_bytes: int = _MAX_PACKET_BYTES


@dataclass
class SupervisorDecision:
    verdict: SupervisorVerdict
    reasons: list[str] = field(default_factory=list)
    unresolved_requirements: list[str] = field(default_factory=list)
    evidence_checked: list[str] = field(default_factory=list)
    model: str | None = None
    decided_at: str | None = None
    schema_version: str = SUPERVISOR_SCHEMA_VERSION
    schema_kind: str = SCHEMA_KIND
    error: str | None = None
    raw_verdict: str | None = None

    @property
    def is_pass(self) -> bool:
        return self.verdict is SupervisorVerdict.PASS

    def to_dict(self) -> dict[str, Any]:
        d = {
            "schema_version": self.schema_version, "schema_kind": self.schema_kind,
            "verdict": self.verdict.value, "reasons": self.reasons,
            "unresolved_requirements": self.unresolved_requirements,
            "evidence_checked": self.evidence_checked, "model": self.model,
            "decided_at": self.decided_at, "error": self.error,
        }
        return d


# The supervisor never trusts the worker's self-assessment. It decides ONLY from
# the evidence in the packet.
SUPERVISOR_SYSTEM = (
    "You are an INDEPENDENT senior engineering verifier for a local lab. You did "
    "NOT write the code under review and you must not trust any 'success' claim "
    "made by the implementer. Decide the outcome ONLY from the supplied evidence "
    "(requirements, acceptance_criteria, diff, tests_run, test_results, "
    "verification, failure_classification, changed_files). "
    "Return ONE JSON object with keys: "
    "verdict (one of PASS, REPAIR, ESCALATE, ABSTAIN), "
    "reasons (array of plain strings), "
    "unresolved_requirements (array of plain strings), "
    "evidence_checked (array of plain strings). "
    "Rules: PASS only if every acceptance criterion is supported by concrete "
    "evidence and all requested tests actually ran and passed. REPAIR if the "
    "implementation is close but a criterion is unmet or a test failed. ESCALATE "
    "if the problem is beyond routine scope (architecture/security/ambiguous "
    "authority) or exceeds allowed attempts. ABSTAIN if the requirements are "
    "genuinely ambiguous or evidence is insufficient to decide. Never invent "
    "evidence. Never PASS on missing/failed/absent tests. Return JSON only."
)


def _screen_packet(packet: dict[str, Any], packet_json: str) -> None:
    """Refuse to transmit a packet that contains credential-shaped material.

    Screening runs against the packet STRUCTURE (via ``supervisor_screen``) rather
    than only its serialized text, so that Python source evidence can be classified
    with AST context: a credential keyword inside a regex pattern literal is a
    pattern definition, while ``api_key = "..."`` is a value. The flat
    ``_APIKEY_RE`` check is retained on the serialized form as a context-free
    backstop — it cannot be exempted by any structural rule.

    ``prod_evidence._detect_secret`` is deliberately NOT applied here. It guards a
    different boundary (production runtime evidence admission) where no source
    structure exists to reason about, and it remains unchanged for that purpose.
    Applying it to source-code review is what starved the Northstar 0B.1 reviewer
    of the security evidence it needed. See docs/EW0A_SUPERVISOR_SCREEN.md."""
    result = supervisor_screen.screen_packet(packet)
    if result.blocked:
        rules = sorted({f.rule for f in result.findings})
        raise SupervisorError(
            f"packet contains secret-like material; refusing to transmit (rules: {rules})")
    if _APIKEY_RE.search(packet_json):
        raise SupervisorError(
            "packet contains secret-like material; refusing to transmit (rules: "
            "['provider_api_key_backstop'])")


SupervisorTransport = Callable[[dict[str, Any]], dict[str, Any]]


def _default_transport(cfg: SupervisorConfig) -> SupervisorTransport:
    """Real OpenAI transport. Reads the key from cfg.key_file ONLY here, ONLY for
    the Authorization header. The key never enters the request body/log."""
    def _run(body: dict[str, Any]) -> dict[str, Any]:
        if not cfg.key_file:
            raise SupervisorError("no supervisor key_file configured")
        try:
            with open(cfg.key_file, "r", encoding="utf-8") as fh:
                key = fh.read().strip()
        except OSError as e:
            raise SupervisorError(f"supervisor key unreadable: {type(e).__name__}")
        if not key:
            raise SupervisorError("supervisor key file empty")
        req = urllib.request.Request(
            cfg.api_base.rstrip("/") + "/chat/completions",
            data=json.dumps(body).encode("utf-8"),
            headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"},
            method="POST")
        with urllib.request.urlopen(req, timeout=cfg.timeout) as r:
            raw = r.read(_MAX_RESPONSE_BYTES + 1024)
        return json.loads(raw.decode("utf-8", "replace"))
    return _run


def _extract_json(text: str) -> dict[str, Any]:
    s = (text or "").strip()
    if s.startswith("```"):
        s = s.split("\n", 1)[1] if "\n" in s else s
        if s.endswith("```"):
            s = s[:-3]
        s = s.strip()
    try:
        obj = json.loads(s)
        if isinstance(obj, dict):
            return obj
    except json.JSONDecodeError:
        pass
    start = s.find("{")
    if start < 0:
        raise SupervisorError("no JSON object in supervisor response")
    depth = 0
    for i in range(start, len(s)):
        if s[i] == "{":
            depth += 1
        elif s[i] == "}":
            depth -= 1
            if depth == 0:
                try:
                    obj = json.loads(s[start:i + 1])
                except json.JSONDecodeError as e:
                    raise SupervisorError(f"malformed supervisor JSON: {e}")
                if isinstance(obj, dict):
                    return obj
                break
    raise SupervisorError("unbalanced JSON in supervisor response")


def _strlist(v: Any) -> list[str]:
    if not isinstance(v, list):
        return []
    return [str(x)[:2000] for x in v[:50] if isinstance(x, (str, int, float))]


def review(packet: dict[str, Any], cfg: SupervisorConfig, now_fn: Callable[[], str],
           *, transport: SupervisorTransport | None = None) -> SupervisorDecision:
    """Send the supervisor packet to GPT and return its independent verdict.
    Fail closed to SUPERVISOR_UNAVAILABLE on any transport/auth/parse error."""
    decided_at = now_fn()
    try:
        packet_json = json.dumps(packet, ensure_ascii=True, sort_keys=True, default=str)
    except (TypeError, ValueError) as e:
        return SupervisorDecision(SupervisorVerdict.SUPERVISOR_UNAVAILABLE,
                                  error=f"unserializable packet: {e}", decided_at=decided_at)
    if len(packet_json.encode("utf-8")) > cfg.max_packet_bytes:
        return SupervisorDecision(SupervisorVerdict.SUPERVISOR_UNAVAILABLE,
                                  error="packet exceeds size bound", decided_at=decided_at)
    try:
        _screen_packet(packet, packet_json)
    except SupervisorError as e:
        return SupervisorDecision(SupervisorVerdict.SUPERVISOR_UNAVAILABLE,
                                  error=str(e), decided_at=decided_at)

    body = {
        "model": cfg.model,
        "messages": [
            {"role": "system", "content": SUPERVISOR_SYSTEM},
            {"role": "user", "content": packet_json},
        ],
        "max_completion_tokens": cfg.max_completion_tokens,
        "response_format": {"type": "json_object"},
    }
    run = transport or _default_transport(cfg)
    try:
        resp = run(body)
    except (SupervisorError, urllib.error.URLError, urllib.error.HTTPError,
            TimeoutError, OSError, ValueError) as e:
        return SupervisorDecision(SupervisorVerdict.SUPERVISOR_UNAVAILABLE,
                                  error=f"supervisor link failed: {type(e).__name__}",
                                  model=cfg.model, decided_at=decided_at)
    try:
        content = resp["choices"][0]["message"]["content"]
        obj = _extract_json(content)
        raw = str(obj.get("verdict", "")).strip().upper()
        verdict = SupervisorVerdict(raw)
    except (KeyError, IndexError, TypeError, ValueError, SupervisorError) as e:
        return SupervisorDecision(SupervisorVerdict.SUPERVISOR_UNAVAILABLE,
                                  error=f"unparseable supervisor decision: {type(e).__name__}",
                                  model=cfg.model, decided_at=decided_at,
                                  raw_verdict=locals().get("raw"))
    if verdict not in _DECISION_VERDICTS:
        return SupervisorDecision(SupervisorVerdict.SUPERVISOR_UNAVAILABLE,
                                  error=f"supervisor returned non-decision verdict: {verdict}",
                                  model=cfg.model, decided_at=decided_at)
    return SupervisorDecision(
        verdict=verdict,
        reasons=_strlist(obj.get("reasons")),
        unresolved_requirements=_strlist(obj.get("unresolved_requirements")),
        evidence_checked=_strlist(obj.get("evidence_checked")),
        model=resp.get("model") or cfg.model, decided_at=decided_at, raw_verdict=raw)
