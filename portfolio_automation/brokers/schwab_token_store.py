# portfolio_automation/brokers/schwab_token_store.py
"""Single-writer, atomic, owner-only persistence for the Schwab OAuth token.

The token file is the one piece of MUTABLE secret state the read-only broker
layer owns. Before this module it was written with a bare ``write_text`` at a
path inside the checkout, with no lock, so two processes (cron sync + an
operator re-auth, or two overlapping syncs) could interleave a refresh and
persist a token Schwab had already rotated away from. This module makes the
store a single logical writer:

* configurable path (``SCHWAB_TOKEN_PATH``); the legacy default is preserved
  for compatibility and the production recommendation is a path OUTSIDE the
  release checkout (see ``RECOMMENDED_PRODUCTION_PATH``);
* owner-only permissions (file 0600, directories we create 0700);
* atomic writes: temp file in the same directory, fsync, ``os.replace`` — an
  interrupted write never leaves a truncated token and never clobbers a good one;
* an advisory exclusive lock (sidecar ``<token>.lock``) so refresh+persist is
  serialized across processes; bounded wait, never an infinite spin;
* nothing here prints, logs or formats token contents. ``repr`` shows the path
  only. Fingerprints are one-way sha256 and are never reversible.

This module has no network access and no trading surface. It moves no
production token: migration to a new path is an explicit operator action.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional

TOKEN_PATH_ENV = "SCHWAB_TOKEN_PATH"
#: Recommended production location — outside any release checkout, so a
#: pointer swap between releases never changes which token file is live.
RECOMMENDED_PRODUCTION_PATH = "/var/lib/stockbot/broker/schwab/token.json"

TOKEN_ABSENT = "absent"
TOKEN_PRESENT = "present"
TOKEN_CORRUPT = "corrupt"
TOKEN_UNREADABLE = "unreadable"

_LOCK_POLL_SEC = 0.05
_FINGERPRINT_DOMAIN = b"stockbot.schwab.token.fingerprint.v1:"


class TokenStoreError(RuntimeError):
    """Base class for token-store failures. Messages never carry token bytes."""


class TokenStoreLocked(TokenStoreError):
    """Another writer holds the token lock and the bounded wait elapsed."""


def resolve_token_path(default: Path | str) -> Path:
    """The effective token path: ``SCHWAB_TOKEN_PATH`` when set, else ``default``.

    Resolved at CALL time (not import time) so an operator's environment and a
    test's monkeypatched default are both honoured without a restart."""
    override = os.environ.get(TOKEN_PATH_ENV, "").strip()
    return Path(override).expanduser() if override else Path(default)


def token_path_is_overridden() -> bool:
    return bool(os.environ.get(TOKEN_PATH_ENV, "").strip())


def fingerprint(value: Optional[str]) -> Optional[str]:
    """One-way, domain-separated sha256 prefix of a secret — for telemetry
    that must be able to say "rotated / not rotated" without ever being able
    to say what the secret was. 16 hex chars; never reversible."""
    if not value:
        return None
    digest = hashlib.sha256(_FINGERPRINT_DOMAIN + str(value).encode("utf-8")).hexdigest()
    return digest[:16]


class TokenStore:
    """Owner-only, atomic, lockable persistence for ONE token file."""

    def __init__(self, path: Path | str) -> None:
        self._path = Path(path)

    # -- identity ---------------------------------------------------------
    @property
    def path(self) -> Path:
        return self._path

    @property
    def lock_path(self) -> Path:
        return self._path.with_name(self._path.name + ".lock")

    def __repr__(self) -> str:  # never the contents
        return f"TokenStore(path={str(self._path)!r})"

    def exists(self) -> bool:
        return self._path.is_file()

    # -- reads ------------------------------------------------------------
    def load_state(self) -> tuple[Optional[dict], str]:
        """``(token, state)`` where state is one of the TOKEN_* constants.

        Distinguishes ABSENT (a legitimate never-authorized state) from
        CORRUPT / UNREADABLE (a real defect an operator must see), which a
        plain ``None`` return cannot."""
        try:
            raw = self._path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None, TOKEN_ABSENT
        except OSError:
            return None, TOKEN_UNREADABLE
        try:
            data = json.loads(raw)
        except ValueError:
            return None, TOKEN_CORRUPT
        if not isinstance(data, dict):
            return None, TOKEN_CORRUPT
        return data, TOKEN_PRESENT

    def load(self) -> Optional[dict]:
        """Compatibility read: the token dict, or None for absent/corrupt."""
        return self.load_state()[0]

    # -- writes -----------------------------------------------------------
    def _ensure_parent(self) -> None:
        parent = self._path.parent
        if not parent.exists():
            parent.mkdir(parents=True, exist_ok=True)
            try:
                os.chmod(parent, 0o700)
            except OSError:
                pass

    def save(self, token: dict) -> None:
        """Atomically persist ``token`` with mode 0600.

        Callers that must serialize against other writers wrap this in
        :meth:`lock`; ``save`` itself does not lock so a holder of the lock can
        call it without deadlocking on its own lock."""
        if not isinstance(token, dict):
            raise TokenStoreError("token must be a mapping")
        self._ensure_parent()
        payload = json.dumps(token)
        fd, tmp_name = tempfile.mkstemp(
            dir=str(self._path.parent), prefix=f".{self._path.name}.", suffix=".tmp")
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(payload)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_name, self._path)
        except BaseException:
            try:
                if os.path.exists(tmp_name):
                    os.unlink(tmp_name)
            except OSError:
                pass
            raise
        try:
            os.chmod(self._path, 0o600)
        except OSError:
            pass

    # -- single writer ----------------------------------------------------
    @contextmanager
    def lock(self, timeout: float = 10.0) -> Iterator[None]:
        """Exclusive advisory lock on the sidecar lock file. Bounded wait.

        Raises :class:`TokenStoreLocked` when the lock is not acquired within
        ``timeout`` seconds — callers classify that as a transient
        "store busy" condition, never as an authorization failure."""
        self._ensure_parent()
        fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            deadline = time.monotonic() + max(0.0, float(timeout))
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise TokenStoreLocked(
                            f"token store busy: {self.lock_path.name} held by another writer")
                    time.sleep(_LOCK_POLL_SEC)
            try:
                yield
            finally:
                try:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                except OSError:
                    pass
        finally:
            os.close(fd)
