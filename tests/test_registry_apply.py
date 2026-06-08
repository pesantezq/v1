"""
Tests for backtesting/registry_apply.py — governed apply path (Pattern-Loop
Step 5; 🔒 PROTECTED mechanism, built inert).

This is the ONLY layer that may change signal_registry.yaml weights, and through
them how decisions are computed. It is therefore built with hard guardrails and
proven here against TEMP COPIES of the registry only — the live
config/signal_registry.yaml is never mutated by these tests. Coverage:
  - default-inert: with no approval file, apply is a no-op (the live-safety gate)
  - apply within caps changes ONLY the approved signal_id's default_weight
  - reject over-cap deltas and unknown signal_ids (registry untouched)
  - revert restores the prior registry byte-for-byte
  - an audit record lands in the POLICY namespace
  - structural guarantee: the module never imports decision/scoring logic, so it
    cannot alter the six protected score semantics (it only edits config data)

Per CLAUDE.md, a full decision_engine VALUE regression (proving the six protected
scores keep their semantics with the new weights) must additionally be run on the
dependency-complete operator environment before any real Step 5 apply is merged.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from portfolio_automation.signal_registry import load_signal_registry

from backtesting.registry_apply import apply_approved_changes, revert_last

_LIVE_REGISTRY = "config/signal_registry.yaml"
_NOW = "2026-06-02T12:00:00+00:00"


def _baseline_weights() -> dict[str, float]:
    return {d.signal_id: d.default_weight for d in load_signal_registry(_LIVE_REGISTRY).all()}


def _temp_registry(tmp_path: Path) -> Path:
    dst = tmp_path / "signal_registry.yaml"
    shutil.copy(_LIVE_REGISTRY, dst)
    return dst


def _write_approval(tmp_path: Path, changes: list[dict]) -> Path:
    p = tmp_path / "approved_weight_changes.json"
    p.write_text(json.dumps({"approved_by": "owner", "changes": changes}), encoding="utf-8")
    return p


def _apply(reg: Path, approval: Path, tmp_path: Path, **kw):
    return apply_approved_changes(
        registry_path=str(reg), approval_path=str(approval),
        history_dir=str(tmp_path / "history"), base_dir=str(tmp_path / "out"),
        now_iso=_NOW, **kw,
    )


def _weights(reg: Path) -> dict[str, float]:
    return {d.signal_id: d.default_weight for d in load_signal_registry(str(reg)).all()}


# --------------------------------------------------------------------------
# Default-inert live-safety gate
# --------------------------------------------------------------------------

def test_no_approval_file_is_inert(tmp_path):
    # Inert when the approval file is absent — proven against a temp registry so the
    # live config/signal_registry.yaml can never be touched (it is the apply DEFAULT).
    reg = _temp_registry(tmp_path)
    before = reg.read_bytes()
    missing_approval = tmp_path / "approved_weight_changes.json"  # does not exist
    rep = apply_approved_changes(
        registry_path=str(reg), approval_path=str(missing_approval),
        history_dir=str(tmp_path / "history"), base_dir=str(tmp_path / "out"),
        now_iso=_NOW)
    assert rep["status"] == "no_approval_file"
    assert reg.read_bytes() == before, "with no approval file, the registry must be untouched"


# --------------------------------------------------------------------------
# Apply within caps
# --------------------------------------------------------------------------

def test_apply_within_caps_changes_only_approved(tmp_path):
    reg = _temp_registry(tmp_path)
    approval = _write_approval(tmp_path, [{"signal_id": "STRONG_MOVE_UP", "delta": 0.05}])
    rep = _apply(reg, approval, tmp_path, max_abs_delta=0.05)
    assert rep["status"] == "applied"
    baseline = _baseline_weights()
    after = _weights(reg)
    assert abs(after["STRONG_MOVE_UP"] - (baseline["STRONG_MOVE_UP"] + 0.05)) < 1e-9  # baseline + delta (baseline-relative; owner-applied weight drifts)
    for sid, w in after.items():
        if sid != "STRONG_MOVE_UP":
            assert w == baseline[sid], f"{sid} weight changed but was not approved"
    for c in rep["applied"]:
        assert 0.0 <= c["new_weight"] <= 1.0


def test_post_apply_registry_still_valid(tmp_path):
    reg = _temp_registry(tmp_path)
    approval = _write_approval(tmp_path, [{"signal_id": "STRONG_MOVE_DOWN", "delta": -0.05}])
    _apply(reg, approval, tmp_path)
    # Reloading must not raise; the edited weight is valid and in range.
    assert load_signal_registry(str(reg)).require("STRONG_MOVE_DOWN").default_weight == 0.40


# --------------------------------------------------------------------------
# Guardrails — refuse, registry untouched
# --------------------------------------------------------------------------

def test_rejects_over_cap_delta(tmp_path):
    reg = _temp_registry(tmp_path)
    before = reg.read_bytes()
    approval = _write_approval(tmp_path, [{"signal_id": "STRONG_MOVE_UP", "delta": 0.10}])
    rep = _apply(reg, approval, tmp_path, max_abs_delta=0.05)
    assert rep["status"] == "no_valid_changes"
    assert any(r["reason"].startswith("magnitude_exceeded") for r in rep["rejected"])
    assert reg.read_bytes() == before  # nothing written


def test_rejects_unknown_signal(tmp_path):
    reg = _temp_registry(tmp_path)
    before = reg.read_bytes()
    approval = _write_approval(tmp_path, [{"signal_id": "NOT_A_SIGNAL", "delta": 0.02}])
    rep = _apply(reg, approval, tmp_path)
    assert rep["status"] == "no_valid_changes"
    assert any(r["reason"] == "unknown_signal" for r in rep["rejected"])
    assert reg.read_bytes() == before


# --------------------------------------------------------------------------
# Revert
# --------------------------------------------------------------------------

def test_revert_restores_byte_identical(tmp_path):
    reg = _temp_registry(tmp_path)
    original = reg.read_bytes()
    approval = _write_approval(tmp_path, [{"signal_id": "STRONG_MOVE_UP", "delta": 0.05}])
    _apply(reg, approval, tmp_path)
    assert reg.read_bytes() != original  # apply did change it
    rep = revert_last(registry_path=str(reg), history_dir=str(tmp_path / "history"),
                      base_dir=str(tmp_path / "out"), now_iso=_NOW)
    assert rep["status"] == "reverted"
    assert reg.read_bytes() == original  # snapshot restored exactly


# --------------------------------------------------------------------------
# Audit + protected-semantics structural guard
# --------------------------------------------------------------------------

def test_audit_record_written_to_policy(tmp_path):
    reg = _temp_registry(tmp_path)
    approval = _write_approval(tmp_path, [{"signal_id": "STRONG_MOVE_UP", "delta": 0.05}])
    _apply(reg, approval, tmp_path)
    audit = tmp_path / "out" / "policy" / "registry_apply_audit.json"
    assert audit.exists()
    entries = json.loads(audit.read_text())
    assert isinstance(entries, list) and entries
    last = entries[-1]
    assert last["approved_by"] == "owner"
    assert last["changes"][0]["signal_id"] == "STRONG_MOVE_UP"
    assert last["snapshot"]


def test_module_never_imports_decision_or_scoring_logic():
    # The apply layer edits registry config DATA only; it must not IMPORT any
    # decision/scoring/recommendation module, so it cannot change score semantics.
    # (Inspect actual import statements, not docstring prose.)
    import ast

    tree = ast.parse(Path("backtesting/registry_apply.py").read_text(encoding="utf-8"))
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            imported.append(node.module or "")
            imported += [f"{node.module}.{a.name}" for a in node.names]

    forbidden = ("decision_engine", "scoring", "recommendation")
    offenders = [m for m in imported if any(f in m for f in forbidden)]
    assert not offenders, f"registry_apply must not import decision/scoring logic; found {offenders}"
