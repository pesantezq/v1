"""Deterministic transport digest of a VS-002 evidence package: ``vs002.transport_digest.v1``.

WHAT THIS IS. A platform-independent identity of the BYTES of a transported
package directory -- the exact regular files the package contract expects, and
nothing else. It exists so that "the package the lab holds is byte-for-byte the
package production published" is a recomputable claim with a committed
algorithm, rather than a one-off shell pipeline.

WHAT THIS IS NOT. It is not a semantic validator (``consumer.validate`` is), it
does not redefine ``package_id`` or the artifact-level canonical JSON digests
(``contracts.artifact_digest``), and it never parses-then-reserializes an
artifact for hashing: every artifact is hashed as the raw bytes on disk. The
manifest is parsed ONCE, and only to decide which artifact set the package
declares (``consumer.expected_artifacts``); its bytes are still hashed raw.

THE ALGORITHM (``vs002.transport_digest.v1``), byte-exact, so another
implementation can reproduce it without reading this file:

    digest = SHA-256(
        DOMAIN_SEPARATION_PREFIX
        || for each artifact, in ascending order of its normalized path:
               u32be( len(path_utf8) ) || path_utf8
            || u64be( len(file_bytes) ) || file_bytes
    )

    DOMAIN_SEPARATION_PREFIX = b"vs002.transport_digest.v1\\x00"
                             (the ASCII algorithm id followed by one NUL byte)
    path_utf8   = the artifact's normalized relative POSIX path (NFC, forward
                  slashes, no ``.``/``..`` segments, not absolute) encoded UTF-8
    ordering    = ascending by Unicode code point of the normalized path, which
                  is identical to ascending byte order of ``path_utf8``
    u32be/u64be = unsigned 32-/64-bit big-endian length prefixes
    file_bytes  = the artifact's raw bytes exactly as stored, streamed

The covered artifact set is EXACTLY ``consumer.expected_artifacts(manifest)``:
``manifest.json`` plus the artifacts the manifest declares. A missing expected
artifact, an unexpected file, a directory or symlink where a regular file is
expected, a path that normalizes outside the package or collides with another
after normalization, or a file whose size changes while it is being read all
FAIL CLOSED (``TransportDigestError``). Filesystem metadata -- mtimes,
permissions, ownership, directory listing order, archive format -- never enters
the hash.

THE LEGACY VALUE. The preregistration and the authoritative state record a
historical transport digest, ``LEGACY_TRANSPORT_DIGEST`` (``4e1f5a6f...``), for
the frozen package ``vs002evd_77469725...``. That value is a RECORDED HISTORICAL
FACT and is preserved unchanged. The procedure that produced it was never
committed to this repository and is classified
``LEGACY_TRANSPORT_DIGEST_ALGORITHM_UNRECOVERABLE``: it is NOT claimed that
``vs002.transport_digest.v1`` reproduces it, and :func:`verify_transport_digest`
refuses to treat it as a recomputable verification target. Establishing a new
canonical transport identity for the real package is a separate, separately
authorized mission; nothing in this module opens that package.

``experimental_noncanonical``.
"""
from __future__ import annotations

import hashlib
import json
import os
import stat
import unicodedata
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Iterator, Mapping, Optional, Union

from portfolio_automation.vs002_evidence.builder import MANIFEST_REL
from portfolio_automation.vs002_evidence.consumer import expected_artifacts

__all__ = [
    "TRANSPORT_DIGEST_ALGORITHM",
    "DOMAIN_SEPARATION_PREFIX",
    "LEGACY_TRANSPORT_DIGEST",
    "LEGACY_TRANSPORT_DIGEST_ALGORITHM",
    "TransportDigestError",
    "TransportDigestMismatch",
    "TransportArtifact",
    "TransportDigest",
    "normalize_relative_path",
    "frame_artifact_header",
    "digest_of_entries",
    "compute_transport_digest",
    "verify_transport_digest",
]

#: Explicit algorithm identity. Any change to the framing below is a NEW version.
TRANSPORT_DIGEST_ALGORITHM = "vs002.transport_digest.v1"
#: Fixed domain-separation prefix: the algorithm id as ASCII, then one NUL byte.
DOMAIN_SEPARATION_PREFIX = TRANSPORT_DIGEST_ALGORITHM.encode("ascii") + b"\x00"
#: Length-prefix widths, in bytes, big-endian, unsigned.
PATH_LENGTH_BYTES = 4
CONTENT_LENGTH_BYTES = 8
#: Streaming read size. Not part of the algorithm: the digest is identical for
#: any chunk size because only the bytes are hashed, never the chunking.
CHUNK_BYTES = 1024 * 1024

#: Historical recorded transport digest of the frozen package
#: vs002evd_77469725f5592e6df33742b68a31ae1e (see .agent/phase_status.yaml and
#: evals/vertical_slice/VS-002_preregistration.json). Preserved as a FACT.
LEGACY_TRANSPORT_DIGEST = (
    "4e1f5a6f432e6b1df7d062ab878922afa1439834f151c0bbfa214c289c216136")
#: Classification of the procedure that produced LEGACY_TRANSPORT_DIGEST: it
#: was never committed and cannot be recomputed. It is not v1 and v1 does not
#: claim to reproduce it.
LEGACY_TRANSPORT_DIGEST_ALGORITHM = "LEGACY_TRANSPORT_DIGEST_ALGORITHM_UNRECOVERABLE"


class TransportDigestError(ValueError):
    """Refusal to produce a transport digest. Never a warning."""


class TransportDigestMismatch(TransportDigestError):
    """The recomputed v1 digest differs from the expected v1 digest."""


@dataclass(frozen=True)
class TransportArtifact:
    path: str
    byte_length: int


@dataclass(frozen=True)
class TransportDigest:
    algorithm: str
    sha256: str
    artifacts: tuple[TransportArtifact, ...]

    @property
    def total_bytes(self) -> int:
        return sum(a.byte_length for a in self.artifacts)

    def to_dict(self) -> dict[str, Any]:
        return {
            "algorithm": self.algorithm,
            "sha256": self.sha256,
            "artifact_count": len(self.artifacts),
            "total_bytes": self.total_bytes,
            "artifacts": [{"path": a.path, "byte_length": a.byte_length}
                          for a in self.artifacts],
        }


# ---------------------------------------------------------------------------
# path normalization
# ---------------------------------------------------------------------------

def normalize_relative_path(name: Any) -> str:
    """Return the canonical relative POSIX form of an artifact path, or refuse.

    Canonical = NFC-normalized, forward slashes only, no empty / ``.`` / ``..``
    segments, not absolute, no NUL. The returned string is what gets hashed and
    what ordering is computed over."""
    if not isinstance(name, str) or not name:
        raise TransportDigestError(f"artifact path must be a non-empty string: {name!r}")
    if "\x00" in name:
        raise TransportDigestError("artifact path contains NUL")
    if "\\" in name:
        raise TransportDigestError(
            f"artifact path contains a backslash (not a POSIX separator): {name!r}")
    norm = unicodedata.normalize("NFC", name)
    if norm.startswith("/"):
        raise TransportDigestError(f"artifact path is absolute: {name!r}")
    parts = norm.split("/")
    if any(p in ("", ".", "..") for p in parts):
        raise TransportDigestError(
            f"artifact path has an empty, '.' or '..' segment (path escape): {name!r}")
    canonical = PurePosixPath(*parts).as_posix()
    if canonical != norm:
        raise TransportDigestError(f"artifact path is not in canonical form: {name!r}")
    return canonical


def frame_artifact_header(path: str, byte_length: int) -> bytes:
    """``u32be(len(path_utf8)) || path_utf8 || u64be(byte_length)``.

    The caller appends the raw bytes themselves. Exposed so the framing can be
    tested and reproduced independently of file I/O."""
    encoded = normalize_relative_path(path).encode("utf-8")
    if len(encoded) > (1 << (8 * PATH_LENGTH_BYTES)) - 1:
        raise TransportDigestError("artifact path too long to frame")
    if byte_length < 0 or byte_length > (1 << (8 * CONTENT_LENGTH_BYTES)) - 1:
        raise TransportDigestError("artifact length out of range")
    return (len(encoded).to_bytes(PATH_LENGTH_BYTES, "big") + encoded
            + byte_length.to_bytes(CONTENT_LENGTH_BYTES, "big"))


# ---------------------------------------------------------------------------
# the hashing core (shared by the in-memory and the streaming path)
# ---------------------------------------------------------------------------

def _order(paths: Iterable[str]) -> list[str]:
    """Ascending by code point == ascending by UTF-8 bytes. Duplicates after
    normalization are a refusal, never silently collapsed."""
    seen: dict[str, str] = {}
    for raw in paths:
        norm = normalize_relative_path(raw)
        if norm in seen:
            raise TransportDigestError(
                f"duplicate artifact path after normalization: {raw!r} and {seen[norm]!r}")
        seen[norm] = raw
    return sorted(seen)


def _hash_ordered(ordered: Iterable[tuple[str, int, Iterator[bytes]]]) -> TransportDigest:
    h = hashlib.sha256()
    h.update(DOMAIN_SEPARATION_PREFIX)
    artifacts: list[TransportArtifact] = []
    for path, length, chunks in ordered:
        h.update(frame_artifact_header(path, length))
        read = 0
        for chunk in chunks:
            h.update(chunk)
            read += len(chunk)
        if read != length:
            raise TransportDigestError(
                f"{path}: declared {length} bytes but {read} were hashed "
                f"(file changed while being read?)")
        artifacts.append(TransportArtifact(path=path, byte_length=length))
    return TransportDigest(algorithm=TRANSPORT_DIGEST_ALGORITHM,
                           sha256=h.hexdigest(), artifacts=tuple(artifacts))


def digest_of_entries(entries: Union[Mapping[str, bytes], Iterable[tuple[str, bytes]]]
                      ) -> TransportDigest:
    """The v1 digest of in-memory ``(path, bytes)`` entries.

    This is the reference form of the algorithm: no filesystem, no manifest
    interpretation, no expected-set check -- the caller owns the set. It is what
    :func:`compute_transport_digest` must equal for the same bytes, and what a
    second implementation can be tested against."""
    items = list(entries.items()) if isinstance(entries, Mapping) else list(entries)
    by_norm: dict[str, bytes] = {}
    for raw, data in items:
        if not isinstance(data, (bytes, bytearray, memoryview)):
            raise TransportDigestError(f"{raw!r}: entry content must be bytes")
        by_norm[normalize_relative_path(raw)] = bytes(data)
    ordered = _order(raw for raw, _ in items)
    return _hash_ordered((p, len(by_norm[p]), iter((by_norm[p],))) for p in ordered)


# ---------------------------------------------------------------------------
# the filesystem path
# ---------------------------------------------------------------------------

def _open_regular_nofollow(path: Path) -> int:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY  # pragma: no cover - Windows only
    try:
        fd = os.open(str(path), flags)
    except OSError as exc:
        raise TransportDigestError(f"{path.name}: cannot open ({exc.strerror})") from exc
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise TransportDigestError(f"{path.name}: not a regular file")
    except Exception:
        os.close(fd)
        raise
    return fd


def _stream(fd: int) -> Iterator[bytes]:
    try:
        while True:
            chunk = os.read(fd, CHUNK_BYTES)
            if not chunk:
                return
            yield chunk
    finally:
        os.close(fd)


def _listing(root: Path) -> dict[str, Path]:
    """Every directory entry, classified fail-closed: a symlink, directory or
    special file anywhere in the package is a refusal, not an omission."""
    if root.is_symlink() or not root.is_dir():
        raise TransportDigestError(f"package root is not a directory: {root}")
    found: dict[str, Path] = {}
    problems: list[str] = []
    with os.scandir(root) as it:
        for entry in it:
            try:
                norm = normalize_relative_path(entry.name)
            except TransportDigestError as exc:
                problems.append(str(exc))
                continue
            if entry.is_symlink():
                problems.append(f"{entry.name}: symlink refused")
                continue
            if entry.is_dir(follow_symlinks=False):
                problems.append(f"{entry.name}: directory where a regular file is expected")
                continue
            if not entry.is_file(follow_symlinks=False):
                problems.append(f"{entry.name}: not a regular file")
                continue
            if norm in found:
                problems.append(
                    f"duplicate artifact path after normalization: {entry.name!r}")
                continue
            found[norm] = root / entry.name
    if problems:
        raise TransportDigestError("; ".join(sorted(problems)))
    return found


def _expected_set(root: Path, found: Mapping[str, Path]) -> frozenset[str]:
    """Decide the expected artifact set from the manifest -- the ONLY parse this
    module performs, and only for set membership; the manifest bytes are hashed
    raw like every other artifact."""
    if MANIFEST_REL not in found:
        raise TransportDigestError(f"missing artifact(s): ['{MANIFEST_REL}']")
    fd = _open_regular_nofollow(found[MANIFEST_REL])
    raw = b"".join(_stream(fd))
    try:
        manifest = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise TransportDigestError(f"{MANIFEST_REL}: not parseable JSON ({exc})") from exc
    if not isinstance(manifest, dict):
        raise TransportDigestError(f"{MANIFEST_REL}: not a JSON object")
    return frozenset(expected_artifacts(manifest))


def compute_transport_digest(package_dir: Union[str, os.PathLike[str]]) -> TransportDigest:
    """Stream the package and return its ``vs002.transport_digest.v1`` digest.

    Fails closed on: a non-directory root; any symlink, directory or special
    entry; a path that does not normalize or collides after normalization; a
    missing expected artifact; an unexpected file; a size change mid-read."""
    root = Path(package_dir)
    found = _listing(root)
    expected = _expected_set(root, found)
    missing = sorted(expected - set(found))
    extra = sorted(set(found) - expected)
    errors: list[str] = []
    if missing:
        errors.append(f"missing artifact(s): {missing}")
    if extra:
        errors.append(f"unexpected artifact(s): {extra}")
    if errors:
        raise TransportDigestError("; ".join(errors))

    def _ordered() -> Iterator[tuple[str, int, Iterator[bytes]]]:
        for norm in _order(found):
            fd = _open_regular_nofollow(found[norm])
            length = os.fstat(fd).st_size
            yield norm, length, _stream(fd)

    return _hash_ordered(_ordered())


def verify_transport_digest(package_dir: Union[str, os.PathLike[str]],
                            expected_sha256: str, *, algorithm: str) -> TransportDigest:
    """Recompute and compare. The expected value MUST be declared as v1.

    Passing ``LEGACY_TRANSPORT_DIGEST_ALGORITHM`` (or any non-v1 label) is
    refused: the historical ``4e1f5a6f...`` value has no committed algorithm and
    is therefore not a recomputable verification target under this helper."""
    if algorithm == LEGACY_TRANSPORT_DIGEST_ALGORITHM:
        raise TransportDigestError(
            "the legacy transport digest was produced by an unrecoverable, "
            "uncommitted procedure (LEGACY_TRANSPORT_DIGEST_ALGORITHM_UNRECOVERABLE); "
            f"it cannot be verified by {TRANSPORT_DIGEST_ALGORITHM} and is preserved "
            "as a historical recorded value only")
    if algorithm != TRANSPORT_DIGEST_ALGORITHM:
        raise TransportDigestError(
            f"unsupported transport digest algorithm {algorithm!r}; "
            f"this helper computes {TRANSPORT_DIGEST_ALGORITHM!r} only")
    if not isinstance(expected_sha256, str) or len(expected_sha256) != 64 \
            or any(c not in "0123456789abcdef" for c in expected_sha256):
        raise TransportDigestError("expected digest must be 64 lowercase hex characters")
    actual = compute_transport_digest(package_dir)
    if actual.sha256 != expected_sha256:
        raise TransportDigestMismatch(
            f"transport digest mismatch: expected {expected_sha256} != "
            f"recomputed {actual.sha256} ({TRANSPORT_DIGEST_ALGORITHM})")
    return actual
