"""applied_fix_verifier — observe-only verification of recorded fixes.

The daily-tool-analysis skill records fixes it applied into
`data/daily_check_state.json` under `applied_fixes`. On the next run this
module re-checks each fix's machine-checkable `verify` spec against today's
artifacts and classifies it:

    confirmed  — the fix's expected post-condition is observed in artifacts
    regressed  — the original symptom is back (the fix did not hold)
    pending    — not yet observable (e.g. needs >=1 prior day of data)
    manual     — no automated check; operator must eyeball

Observe-only: it READS artifacts and RETURNS verdicts; it writes no output
artifact of its own. The skill consumes the result to (a) surface a body
line, (b) escalate any `regressed` fix to portfolio-discovery-health, and
(c) drop `confirmed` fixes from state so they stop re-checking forever.

Staleness guard: a batch may carry `applied_at` (the ISO timestamp the fix
went live). Artifact-reading checks compare it to the artifact's
`generated_at` and return `pending` ("artifact predates fix") when the
artifact is older — otherwise every fix would false-read `regressed` on its
first run, before the pipeline has regenerated artifacts under the new code.

`verify` spec kinds
-------------------
liveness_row_not_warn:
    {kind, row, regression_below_observed}
    Reads outputs/latest/daily_run_status.json:content_liveness. REGRESSED if
    the named row warns at observed <= regression_below_observed (the old,
    stricter threshold is back). CONFIRMED if the row is ok. PENDING if the
    row warns above the new threshold (a genuinely missed cycle, not a
    regression) or the row/artifact is missing.

artifact_max_field_gt:
    {kind, artifact, list_path, field, threshold}
    CONFIRMED if max(field) across the artifact's dotted list_path exceeds
    threshold. PENDING otherwise — a single snapshot at/below threshold cannot
    distinguish "fix regressed" from "first day of data" (both read 0), so it
    is never reported as REGRESSED.

file_contains:
    {kind, path, contains?, absent?}
    For a TEXT artifact (e.g. outputs/latest/daily_memo.md) where the fix's
    post-condition is a rendered string, not a JSON field. `contains` and
    `absent` may each be a string or a list of strings.
    REGRESSED if any `absent` (regression-marker) string is present — the old
    symptom's text is back. CONFIRMED if all `contains` strings are present
    (and no `absent` marker is). PENDING if the file is missing, predates the
    fix, or a `contains` string is not present yet — a missing `contains`
    marker is never REGRESSED, since absence can't be told apart from a
    legitimately not-applicable state (e.g. first-gauge era with no prior
    gauge to compare). Staleness uses file mtime (the text-file equivalent of
    a JSON artifact's generated_at).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

CONFIRMED = "confirmed"
REGRESSED = "regressed"
PENDING = "pending"
MANUAL = "manual"


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return None


def _dig(obj: Any, dotted: str) -> Any:
    cur = obj
    for key in (dotted or "").split("."):
        if not key:
            continue
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    return cur


def _artifact_is_stale(payload: Any, applied_at: str | None) -> bool:
    """True if the artifact's generated_at predates the fix's applied_at — i.e.
    the artifact still reflects the pre-fix code and cannot judge the fix."""
    if not applied_at or not isinstance(payload, dict):
        return False
    gen = payload.get("generated_at")
    if not gen:
        return False
    try:
        from datetime import datetime
        return datetime.fromisoformat(gen) < datetime.fromisoformat(applied_at)
    except Exception:
        return False


def _file_is_stale(path: Path, applied_at: str | None) -> bool:
    """True if the file's mtime predates the fix's applied_at — the text-file
    equivalent of _artifact_is_stale's generated_at comparison. A file written
    before the fix went live still reflects the pre-fix code and cannot judge
    the fix."""
    if not applied_at:
        return False
    try:
        from datetime import datetime
        return path.stat().st_mtime < datetime.fromisoformat(applied_at).timestamp()
    except Exception:
        return False


def _as_marker_list(value: Any) -> list[str]:
    """Normalise a contains/absent spec field to a list of strings."""
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    return [str(m) for m in value]


def _check_file_contains(spec: dict, root: Path, applied_at: str | None) -> tuple[str, str]:
    rel = spec.get("path", "")
    path = root / rel
    if not path.exists():
        return (PENDING, f"{rel} missing")
    if _file_is_stale(path, applied_at):
        return (PENDING, "file predates fix (mtime < applied_at)")
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception as exc:
        return (PENDING, f"{rel} unreadable: {exc}")

    absent = _as_marker_list(spec.get("absent"))
    contains = _as_marker_list(spec.get("contains"))
    if not absent and not contains:
        return (PENDING, "file_contains: no contains/absent markers specified")

    present_absent = [m for m in absent if m in text]
    if present_absent:
        return (REGRESSED, f"regression marker(s) present in {rel}: {present_absent}")

    missing = [m for m in contains if m not in text]
    if missing:
        return (PENDING, f"marker(s) not present yet in {rel}: {missing}")
    # All `contains` present (or only `absent` markers were specified and none
    # are present) → the fix's post-condition holds.
    detail = (
        f"all marker(s) present in {rel}" if contains
        else f"no regression marker(s) present in {rel}"
    )
    return (CONFIRMED, detail)


def _check_liveness_row_not_warn(spec: dict, root: Path, applied_at: str | None) -> tuple[str, str]:
    payload = _load_json(root / "outputs/latest/daily_run_status.json")
    if not isinstance(payload, dict):
        return (PENDING, "daily_run_status.json missing")
    if _artifact_is_stale(payload, applied_at):
        return (PENDING, "artifact predates fix (generated_at < applied_at)")
    rows = payload.get("content_liveness") or []
    row_name = spec.get("row")
    row = next((r for r in rows if isinstance(r, dict) and r.get("name") == row_name), None)
    if row is None:
        return (PENDING, f"row {row_name!r} not present yet")
    status = row.get("status")
    observed = row.get("observed")
    regress_at = spec.get("regression_below_observed")
    if (
        status == "warn"
        and isinstance(observed, (int, float))
        and regress_at is not None
        and observed <= regress_at
    ):
        return (REGRESSED, f"{row_name} warns at observed={observed} <= {regress_at} (old threshold back)")
    if status == "ok":
        return (CONFIRMED, f"{row_name} ok (observed={observed})")
    return (PENDING, f"{row_name} status={status} observed={observed}")


def _check_artifact_max_field_gt(spec: dict, root: Path, applied_at: str | None) -> tuple[str, str]:
    payload = _load_json(root / spec.get("artifact", ""))
    if payload is None:
        return (PENDING, f"{spec.get('artifact')} missing")
    if _artifact_is_stale(payload, applied_at):
        return (PENDING, "artifact predates fix (generated_at < applied_at)")
    items = _dig(payload, spec.get("list_path", ""))
    if not isinstance(items, list) or not items:
        return (PENDING, f"{spec.get('list_path')} empty")
    field = spec.get("field")
    threshold = spec.get("threshold", 0)
    values = [it.get(field, 0) or 0 for it in items if isinstance(it, dict)]
    mx = max(values) if values else 0
    if mx > threshold:
        return (CONFIRMED, f"max {field}={mx} > {threshold}")
    return (PENDING, f"max {field}={mx} <= {threshold} (not yet observable)")


_CHECKS = {
    "liveness_row_not_warn": _check_liveness_row_not_warn,
    "artifact_max_field_gt": _check_artifact_max_field_gt,
    "file_contains": _check_file_contains,
}


def verify_applied_fixes(state: dict, artifacts_root: str | Path) -> list[dict]:
    """Return a verdict dict per recorded fix:
    {id, date, commit, status, detail}. Empty list if no applied_fixes."""
    root = Path(artifacts_root)
    verdicts: list[dict] = []
    for batch in (state.get("applied_fixes") or []):
        applied_at = batch.get("applied_at")
        for fix in (batch.get("fixes") or []):
            spec = fix.get("verify") or {}
            checker = _CHECKS.get(spec.get("kind"))
            if checker is None:
                status, detail = MANUAL, "no automated check — verify manually"
            else:
                try:
                    status, detail = checker(spec, root, applied_at)
                except Exception as exc:  # never let one bad spec abort the run
                    status, detail = PENDING, f"check error: {exc}"
            verdicts.append({
                "id": fix.get("id"),
                "date": batch.get("date"),
                "commit": batch.get("commit"),
                "status": status,
                "detail": detail,
            })
    return verdicts


def summarize(verdicts: list[dict]) -> dict:
    """Counts by status plus a `has_regression` convenience flag."""
    out = {CONFIRMED: 0, REGRESSED: 0, PENDING: 0, MANUAL: 0}
    for v in verdicts:
        st = v.get("status")
        if st in out:
            out[st] += 1
    out["has_regression"] = out[REGRESSED] > 0
    return out


def drop_resolved(state: dict, verdicts: list[dict]) -> dict:
    """Return state with `confirmed` fixes removed (they held — stop
    re-checking). pending / regressed / manual fixes are retained. Batches
    left with no fixes are dropped. Does not mutate the input."""
    confirmed_ids = {v.get("id") for v in verdicts if v.get("status") == CONFIRMED}
    if not state.get("applied_fixes"):
        return dict(state)
    new_batches = []
    for batch in state["applied_fixes"]:
        kept = [f for f in (batch.get("fixes") or []) if f.get("id") not in confirmed_ids]
        if kept:
            new_batch = dict(batch)
            new_batch["fixes"] = kept
            new_batches.append(new_batch)
    out = dict(state)
    out["applied_fixes"] = new_batches
    return out
