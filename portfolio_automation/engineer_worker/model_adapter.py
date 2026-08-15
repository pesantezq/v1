"""Local model adapter for the Engineer Worker.

The model is reached ONLY through an ``InferenceFn`` — ``(system, user) -> text``.
The real implementation posts to the inference-only Ollama facade (the same
default-deny path certified in Phase 0B); tests inject a deterministic fake. All
model output is size-bounded and parsed defensively; malformed output raises
``ContractError`` (fail-closed).
"""
from __future__ import annotations

import json
from typing import Callable

from portfolio_automation.engineer_worker.contracts import ContractError

InferenceFn = Callable[[str, str], str]

MAX_MODEL_OUTPUT_BYTES = 65_536
DEFAULT_MODEL = "qwen2.5:7b"


def ollama_inference_fn(url: str, model: str = DEFAULT_MODEL, timeout: int = 120) -> InferenceFn:
    """Return an InferenceFn that calls the inference-only Ollama facade.

    Uses /api/generate (allowed by the facade); pull/push/create/delete are
    denied by the facade itself. Never used in hermetic tests."""
    import urllib.request

    def _infer(system: str, user: str) -> str:
        payload = json.dumps({
            "model": model,
            "system": system,
            "prompt": user,
            "stream": False,
            "format": "json",
            "options": {"temperature": 0.0, "num_predict": 1024},
        }).encode("utf-8")
        req = urllib.request.Request(url.rstrip("/") + "/api/generate", data=payload,
                                     headers={"Content-Type": "application/json"},
                                     method="POST")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read(MAX_MODEL_OUTPUT_BYTES + 1024)
        resp = json.loads(body.decode("utf-8", "replace"))
        return str(resp.get("response", ""))

    return _infer


def extract_json_object(text: str) -> dict:
    """Extract a single JSON object from model text (tolerant of ``` fences and
    surrounding prose). Fail-closed on oversize or no valid object."""
    if text is None:
        raise ContractError("model returned no text")
    if len(text.encode("utf-8", "replace")) > MAX_MODEL_OUTPUT_BYTES:
        raise ContractError("model output exceeds size bound")
    s = text.strip()
    if s.startswith("```"):
        # strip a leading ```json / ``` fence and trailing fence
        s = s.split("\n", 1)[1] if "\n" in s else s
        if s.endswith("```"):
            s = s[: -3]
        s = s.strip()
    # Fast path: whole thing is JSON.
    try:
        obj = json.loads(s)
        if isinstance(obj, dict):
            return obj
        raise ContractError("model output is not a JSON object")
    except json.JSONDecodeError:
        pass
    # Fallback: first balanced {...} span.
    start = s.find("{")
    if start < 0:
        raise ContractError("no JSON object in model output")
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(s)):
        c = s[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
        else:
            if c == '"':
                in_str = True
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    try:
                        obj = json.loads(s[start : i + 1])
                    except json.JSONDecodeError as e:
                        raise ContractError(f"malformed JSON object: {e}")
                    if not isinstance(obj, dict):
                        raise ContractError("model output is not a JSON object")
                    return obj
    raise ContractError("unbalanced JSON object in model output")


# --- prompt builders (system prompts pin the contract; no authority granted) --
FINDING_SYSTEM = (
    "You are a read-only engineering diagnostic assistant for a local lab. You "
    "analyze a bounded diagnostic bundle and return ONE JSON object only. You "
    "have NO shell, NO network, NO authority to change anything. Never invent "
    "evidence. Only reference evidence ids listed in 'valid_evidence_refs'. If "
    "the evidence is insufficient or ambiguous, set \"abstain\": true and explain "
    "in \"abstain_reason\"; do not fabricate a cause.\n"
    "Return JSON with keys: summary, severity (INFO|LOW|MEDIUM|HIGH|CRITICAL), "
    "confidence (0..1), observations[], likely_causes[], evidence_refs[], "
    "recommended_checks[], repair_recommended (bool), repair_scope "
    "(NONE|DOCS|TESTS|DEV_TOOLING), abstain (bool), abstain_reason. "
    "ALL list fields (observations, likely_causes, evidence_refs, "
    "recommended_checks) MUST be arrays of PLAIN STRINGS, never objects. "
    "You MAY instead return {\"tool_request\": {\"capability\": <CAP>, \"argument\": <str>}} "
    "to request ONE approved diagnostic, where CAP is one of the allowed "
    "capabilities listed in the bundle. Return JSON only."
)

REPAIR_SYSTEM = (
    "You are a read-only engineering repair assistant for a local lab. Propose a "
    "MINIMAL fix as ONE JSON object. You may only edit files the controller lists "
    "as repairable; never protected paths, never git internals, never security "
    "runtime. Return JSON with keys: rationale, edits (list of {path, "
    "new_content} with FULL file content), tests_to_run (list of allowlisted "
    "pytest targets). Return JSON only."
)


def build_finding_user_prompt(bundle_payload: dict, tool_results: list[dict],
                              allowed_caps: list[str]) -> str:
    return json.dumps({
        "task": "Diagnose the engineering/environment situation from this bundle.",
        "allowed_tool_capabilities": allowed_caps,
        "bundle": bundle_payload,
        "prior_tool_results": tool_results,
        "instructions": "Return a finding JSON, or a single tool_request JSON.",
    }, ensure_ascii=True)


def build_repair_user_prompt(finding: dict, repairable_files: list[dict],
                             allowed_tests: list[str]) -> str:
    return json.dumps({
        "task": "Propose a minimal repair for the diagnosed issue.",
        "finding": finding,
        "repairable_files": repairable_files,   # [{path, content}]
        "allowed_tests": allowed_tests,
        "instructions": "Return a repair proposal JSON editing only repairable files.",
    }, ensure_ascii=True)
