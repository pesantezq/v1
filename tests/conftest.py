"""Session-scoped guard: the protected scoring registry and its approval/history
must NOT be mutated by the test suite. Snapshots them before the session and fails
loudly (and restores) if any test changed them — the canonical example being a test
that calls the apply path against the live config paths."""
from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

_PROTECTED = [
    Path("config/signal_registry.yaml"),
    Path("config/approved_weight_changes.json"),
]
_HISTORY = Path("config/history")


def _hash(p: Path) -> str | None:
    try:
        return hashlib.sha256(p.read_bytes()).hexdigest()
    except FileNotFoundError:
        return None


@pytest.fixture(autouse=True)
def _clear_dashboard_auth_env(monkeypatch):
    """Keep dashboard route-render tests deterministic.

    The daily-memo / email path calls ``load_dotenv()`` (correct in production),
    which writes the operator's real ``GUI_V2_AUTH_USER`` / ``GUI_V2_AUTH_PASS``
    from ``.env`` straight into ``os.environ`` — a direct write monkeypatch cannot
    revert. Once any such test runs, every later route-render test that expects an
    unauthenticated 200 instead gets a 401. Clearing the two vars at the start of
    every test neutralizes that leak; tests that exercise auth set them explicitly
    via their own ``monkeypatch.setenv`` (which runs after this fixture).
    """
    monkeypatch.delenv("GUI_V2_AUTH_USER", raising=False)
    monkeypatch.delenv("GUI_V2_AUTH_PASS", raising=False)


@pytest.fixture(scope="session", autouse=True)
def _protect_scoring_registry():
    before = {p: _hash(p) for p in _PROTECTED}
    before_snaps = set(_HISTORY.glob("signal_registry.*.yaml")) if _HISTORY.is_dir() else set()
    before_bytes = {p: (p.read_bytes() if before[p] is not None else None) for p in _PROTECTED}
    yield
    violations = []
    for p in _PROTECTED:
        after = _hash(p)
        if after != before[p]:
            violations.append(str(p))
            # restore byte-for-byte (or delete if it didn't exist before)
            if before_bytes[p] is None:
                try:
                    p.unlink()
                except FileNotFoundError:
                    pass
            else:
                p.write_bytes(before_bytes[p])
    # remove any history snapshots a test created
    if _HISTORY.is_dir():
        for snap in set(_HISTORY.glob("signal_registry.*.yaml")) - before_snaps:
            snap.unlink()
    assert not violations, (
        f"protected scoring registry mutated by the test suite: {violations} "
        f"(restored). A test applied to the live config paths — make it hermetic.")
