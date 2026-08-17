"""Which execution configuration produced this decision?

WHY THIS EXISTS.

Identity information was real but scattered: a prompt version in the controller's
telemetry dict, a model name on the supervisor decision, a repository SHA in two
places with two different meanings, an authority level recorded nowhere at all.
Each piece was individually reasonable and collectively unusable -- there was no
way to ask "show me the supervisor PASS decisions produced by model X under
prompt version Y" without reconstructing the answer by hand, if at all.

Over a multi-year autonomous program models change, prompts change, toolsets
change and authority changes. Apprenticeship history that cannot name the
configuration that produced it is not evidence; it is anecdote.

HONEST UNKNOWNS ARE A FEATURE.

Providers frequently do not expose a precise build identifier. The correct
record is ``UNAVAILABLE_AT_RECORD_TIME`` -- never a plausible-looking guess and
never the configured default silently standing in for what was actually served.
A later audit must be able to ask "which records came from a model whose exact
version was unavailable?" and get a truthful answer, which is only possible if
that state was preserved rather than filled in.

The sentinel is deliberately not the string ``"legacy"`` or ``"unknown"``: those
could be mistaken for real version identifiers.

IDENTITY MATERIAL vs RECORD METADATA.

``recorded_at`` is metadata, NOT identity material. Two otherwise identical
executions must produce the same ``execution_id``; if the timestamp participated,
every replay would mint a new identity and the whole point -- grouping decisions
by configuration -- would be lost.

SECRETS NEVER ENTER IDENTITY.

Tool configuration is the realistic leak path: it is the one place an API key or
endpoint credential plausibly lives. Only an allowlisted, non-secret projection
is kept, plus a digest. A record that leaked a key would be worse than no record,
because it would be durable, replicated and append-only.
"""
from __future__ import annotations

import hashlib
import json
import re
import urllib.parse
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional

from portfolio_automation.engineer_worker import EXPERIMENTAL_MARKER

SCHEMA_KIND = EXPERIMENTAL_MARKER
EXECUTION_IDENTITY_SCHEMA_VERSION = "engineering.execution_identity.v1"

#: An attribute the producer genuinely could not determine at record time.
#: Distinct from a value that was never asked for, and deliberately not
#: mistakable for a real identifier.
UNAVAILABLE = "UNAVAILABLE_AT_RECORD_TIME"

#: A record written before this schema existed. Its attribution is not missing
#: because of an outage -- it was never captured, and no amount of later
#: inference can recover it.
LEGACY_UNATTRIBUTED = "LEGACY_UNATTRIBUTED"

#: Substrings that mark a value as credential-bearing regardless of its key.
#: DEFENCE IN DEPTH ONLY -- never the primary boundary. A word filter cannot
#: recognise a bare opaque token, which is exactly how ``mode = "sk-..."``
#: survived the first implementation of this screen.
_SECRETISH = ("key", "token", "secret", "password", "authorization", "bearer",
              "credential")

# --- field-specific structural validation ------------------------------------
#
# A field is safe because its CONTRACT constrains what it can contain, not
# because a generic substring filter failed to recognise the value as a secret.
# The first version of this screen relied on the latter and leaked: `mode`,
# `protocol` and `toolset_version` accepted arbitrary short strings, and
# `timeout` accepted a string at all.
#
# Every validator below returns the normalised value, or None to reject.

#: Lowercase hyphenated token: "one-shot", "strict". Rejects uppercase and
#: underscores, which is what excludes "sk-A1b2C3d4", "ghp_..." and "AKIA...".
_TOKEN_RE = re.compile(r"\A[a-z][a-z0-9]*(-[a-z0-9]+)*\Z")
#: Dotted lowercase identifier requiring at least one dot: "gpt_supervisor.review".
#: The mandatory dot is what rejects a bare opaque token like "ghp_zzzz...".
_TOOLSET_RE = re.compile(r"\A[a-z][a-z0-9_]{0,23}(\.[a-z][a-z0-9_]{0,23})+\Z")
#: Version: "v1", "1.2.3" -- or a lowercase token like "one-shot".
_VERSION_RE = re.compile(r"\A(v?\d+(\.\d+){0,3}|[a-z][a-z0-9]*(-[a-z0-9]+)*)\Z")
#: Hostname, optionally with a port. No userinfo, path, query or fragment.
_HOST_RE = re.compile(
    r"\A(?=.{1,253}\Z)[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?"
    r"(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)*(:\d{1,5})?\Z")


#: Single-label hosts that are legitimate without a dot. Anything else must be
#: a dotted name, because a bare label is indistinguishable from an opaque token.
_BARE_HOSTS = frozenset({"localhost"})


def _bounded_int(lo: int, hi: int):
    def _check(value: Any) -> Optional[int]:
        # bool is an int subclass in Python; accepting it here would let True
        # masquerade as 1 and make the recorded configuration misleading.
        if isinstance(value, bool) or not isinstance(value, int):
            return None
        return value if lo <= value <= hi else None
    return _check


def _matching(pattern: "re.Pattern[str]", max_len: int):
    def _check(value: Any) -> Optional[str]:
        if not isinstance(value, str) or len(value) > max_len:
            return None
        return value if pattern.match(value) else None
    return _check


def _host(value: Any) -> Optional[str]:
    """Accept a host, or a URL that carries nothing but a host.

    POLICY: reject outright rather than strip. A URL carrying userinfo, a query
    or a fragment is evidence the caller is passing secret-bearing material
    here; silently keeping the hostname would discard that signal and record a
    value the caller never meant as an identity. Fail closed instead."""
    if not isinstance(value, str) or not value or len(value) > 253:
        return None
    candidate = value.strip()
    if "://" in candidate:
        parsed = urllib.parse.urlsplit(candidate)
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            return None
        if parsed.path not in ("", "/"):
            return None
        candidate = parsed.netloc
    if "@" in candidate or "/" in candidate or "?" in candidate or "#" in candidate:
        return None
    candidate = candidate.lower()
    if not _HOST_RE.match(candidate):
        return None
    # A bare single label is not enough. "sk-a1b2c3d4" is a SYNTACTICALLY VALID
    # hostname, so grammar alone would admit a credential here -- found by the
    # negative test. An API base host is a dotted name, or an explicit local
    # host; anything else is rejected rather than guessed at.
    host = candidate.split(":", 1)[0]
    if "." not in host and host not in _BARE_HOSTS:
        return None
    return candidate


#: Tool-configuration keys safe to record, each with the validator that defines
#: what "safe" means for it. An allowlist, never a denylist: a denylist fails
#: open on the key nobody thought of.
_SAFE_TOOL_FIELDS: dict[str, Any] = {
    "toolset": _matching(_TOOLSET_RE, 64),
    "toolset_version": _matching(_VERSION_RE, 24),
    "protocol": _matching(_TOKEN_RE, 24),
    "mode": _matching(_TOKEN_RE, 24),
    "timeout": _bounded_int(1, 86_400),
    "max_tokens": _bounded_int(1, 1_000_000),
    "max_completion_tokens": _bounded_int(1, 1_000_000),
    "api_base_host": _host,
}
_SAFE_TOOL_KEYS = frozenset(_SAFE_TOOL_FIELDS)


def _canonical(payload: Mapping[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, ensure_ascii=True, separators=(",", ":"))


def safe_toolset_identity(name: str, config: Optional[Mapping[str, Any]] = None
                          ) -> dict[str, Any]:
    """Project tool configuration down to something safe to persist.

    STRUCTURAL VALIDATION IS THE BOUNDARY. Each allowlisted key has a validator
    defining the shape its value may take; anything else is dropped. The earlier
    version relied on a substring filter and leaked, because a bare token like
    ``sk-A1b2C3d4`` contains none of the suspicious words -- and ``timeout``
    accepted a string at all, since nothing enforced its type.

    A credential-pattern check is retained as defence in depth, but it is not
    load-bearing: no claim of reliable secret detection is made or needed."""
    safe: dict[str, Any] = {}
    dropped: list[str] = []
    for key, value in sorted((config or {}).items()):
        name = str(key)
        # The allowlist alone decides which KEYS may be recorded. A substring
        # check on the key name is not only redundant here, it is wrong: it
        # dropped the legitimate `max_tokens` and `max_completion_tokens`
        # because their names contain "token". Screening applies to VALUES.
        validator = _SAFE_TOOL_FIELDS.get(name)
        if validator is None:
            dropped.append(name)
            continue
        checked = validator(value)
        if checked is None:
            dropped.append(name)
            continue
        # Defence in depth, after the structural contract has already passed.
        if isinstance(checked, str) and any(s in checked.lower() for s in _SECRETISH):
            dropped.append(name)
            continue
        safe[name] = checked
    digest = hashlib.sha256(
        _canonical({"toolset": name, **safe}).encode("utf-8")).hexdigest()[:32]
    return {"toolset_id": name, "safe_config": safe,
            "dropped_keys": sorted(dropped), "config_digest": digest}


@dataclass(frozen=True)
class ExecutionIdentity:
    """The configuration that produced one AI execution.

    Every attribute defaults to UNAVAILABLE rather than to a plausible value, so
    a producer that forgets to supply something records that it did not know --
    which is recoverable -- instead of recording something false, which is not.
    """

    # -- worker ------------------------------------------------------------
    worker_id: str = UNAVAILABLE
    worker_role: str = UNAVAILABLE
    authority_level: str = UNAVAILABLE
    # -- model -------------------------------------------------------------
    model_provider: str = UNAVAILABLE
    model_name: str = UNAVAILABLE
    #: The precise build/version the provider actually served. Most APIs do not
    #: expose it; UNAVAILABLE is the honest and expected value.
    model_version: str = UNAVAILABLE
    # -- instruction -------------------------------------------------------
    prompt_version: str = UNAVAILABLE
    instruction_version: str = UNAVAILABLE
    # -- tools -------------------------------------------------------------
    toolset_id: str = UNAVAILABLE
    toolset_digest: str = UNAVAILABLE
    # -- repository --------------------------------------------------------
    #: The tree the execution RAN against.
    repository_sha: str = UNAVAILABLE
    #: The tree a verdict CERTIFIES. Distinct on purpose: a reviewer judges a
    #: frozen candidate, and certification evidence necessarily lands in a later
    #: commit. Collapsing them once made a report claim a PASS covered code the
    #: reviewer never saw.
    candidate_sha: str = UNAVAILABLE
    branch: str = UNAVAILABLE
    # -- task / input ------------------------------------------------------
    task_id: str = UNAVAILABLE
    mission_id: str = UNAVAILABLE
    #: Identity of the input, by reference. Existing hashes are reused rather
    #: than payloads copied.
    input_id: str = UNAVAILABLE
    # -- record metadata (NOT identity material) ---------------------------
    recorded_at: Optional[str] = None
    schema_version: str = EXECUTION_IDENTITY_SCHEMA_VERSION
    schema_kind: str = SCHEMA_KIND
    #: Non-secret tool projection, kept for readability alongside the digest.
    toolset_safe_config: Mapping[str, Any] = field(default_factory=dict)

    #: Ordered, explicit. A field not listed here cannot silently join identity,
    #: and a field listed here cannot silently leave it.
    IDENTITY_FIELDS = (
        "worker_id", "worker_role", "authority_level",
        "model_provider", "model_name", "model_version",
        "prompt_version", "instruction_version",
        "toolset_id", "toolset_digest",
        "repository_sha", "candidate_sha", "branch",
        "task_id", "mission_id", "input_id",
    )

    def identity_material(self) -> dict[str, str]:
        return {name: str(getattr(self, name)) for name in self.IDENTITY_FIELDS}

    def execution_id(self) -> str:
        """Deterministic id over identity material only.

        recorded_at is excluded: the same configuration executing twice is the
        same configuration, and an id that changed per microsecond could never
        group anything."""
        blob = _canonical(self.identity_material()).encode("utf-8")
        return "exid_" + hashlib.sha256(blob).hexdigest()[:32]

    @property
    def is_fully_attributed(self) -> bool:
        return all(v != UNAVAILABLE for v in self.identity_material().values())

    def unavailable_attributes(self) -> tuple[str, ...]:
        return tuple(k for k, v in self.identity_material().items() if v == UNAVAILABLE)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "schema_version": self.schema_version, "schema_kind": self.schema_kind,
            "execution_id": self.execution_id(),
            **self.identity_material(),
            "recorded_at": self.recorded_at,
            "toolset_safe_config": dict(self.toolset_safe_config),
            "unavailable_attributes": list(self.unavailable_attributes()),
        }
        return out

    @classmethod
    def from_dict(cls, data: Optional[Mapping[str, Any]]) -> "ExecutionIdentity":
        """Load a record, including one written before this schema existed.

        A legacy record is marked as such and its attributes stay UNAVAILABLE.
        Nothing is inferred: filling in today's configured model would assert
        that it produced a decision made under a configuration nobody recorded."""
        if not data:
            return cls(worker_id=LEGACY_UNATTRIBUTED, schema_version=LEGACY_UNATTRIBUTED)
        version = data.get("schema_version")
        if version != EXECUTION_IDENTITY_SCHEMA_VERSION:
            if version is None:
                # Predates the schema entirely.
                return cls(worker_id=LEGACY_UNATTRIBUTED,
                           schema_version=LEGACY_UNATTRIBUTED,
                           recorded_at=data.get("recorded_at"))
            raise ValueError(
                f"unknown execution identity schema {version!r}; refusing to "
                "interpret it as the current schema -- a silent misreading would "
                f"attribute records to configurations that never produced them "
                f"(expected {EXECUTION_IDENTITY_SCHEMA_VERSION!r})")
        known = {f for f in cls.IDENTITY_FIELDS}
        return cls(**{k: str(data.get(k, UNAVAILABLE)) for k in known},
                   recorded_at=data.get("recorded_at"),
                   toolset_safe_config=dict(data.get("toolset_safe_config") or {}))


def build_execution_identity(*, worker_id: str = UNAVAILABLE,
                             worker_role: str = UNAVAILABLE,
                             authority_level: str = UNAVAILABLE,
                             model_provider: str = UNAVAILABLE,
                             model_name: str = UNAVAILABLE,
                             model_version: str = UNAVAILABLE,
                             prompt_version: str = UNAVAILABLE,
                             instruction_version: str = UNAVAILABLE,
                             toolset: str = UNAVAILABLE,
                             tool_config: Optional[Mapping[str, Any]] = None,
                             repository_sha: str = UNAVAILABLE,
                             candidate_sha: str = UNAVAILABLE,
                             branch: str = UNAVAILABLE,
                             task_id: str = UNAVAILABLE,
                             mission_id: str = UNAVAILABLE,
                             input_id: str = UNAVAILABLE,
                             recorded_at: Optional[str] = None) -> ExecutionIdentity:
    """Single construction point, so field-building logic is not duplicated
    across producers and so tool configuration is screened exactly once."""
    tools = (safe_toolset_identity(toolset, tool_config)
             if toolset != UNAVAILABLE else
             {"toolset_id": UNAVAILABLE, "safe_config": {},
              "config_digest": UNAVAILABLE})
    return ExecutionIdentity(
        worker_id=worker_id, worker_role=worker_role, authority_level=authority_level,
        model_provider=model_provider, model_name=model_name,
        model_version=model_version, prompt_version=prompt_version,
        instruction_version=instruction_version,
        toolset_id=tools["toolset_id"], toolset_digest=tools["config_digest"],
        toolset_safe_config=tools["safe_config"],
        repository_sha=repository_sha, candidate_sha=candidate_sha, branch=branch,
        task_id=task_id, mission_id=mission_id, input_id=input_id,
        recorded_at=recorded_at)
