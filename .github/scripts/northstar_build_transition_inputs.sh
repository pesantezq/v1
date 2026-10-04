#!/bin/bash
# Emit NORTHSTAR transition-proposer inputs JSON (stdout) from TRUSTED main state.
# Args: <certified_sha> <ci_conclusion> <ci_event> <ci_branch>
# IO glue (runs only post-merge on main). Authority is read fail-closed via
# roadmap_guard; the paused authorization is read verbatim from phase_status.
set -uo pipefail
SHA="${1:?certified sha}"; CONCL="${2:-success}"; EVENT="${3:-push}"; BRANCH="${4:-main}"
python - "$SHA" "$CONCL" "$EVENT" "$BRANCH" <<'PY'
import json, sys, pathlib
sha, concl, event, branch = sys.argv[1:5]
root = pathlib.Path(".").resolve()
try:
    import yaml
    reg = yaml.safe_load(pathlib.Path(".agent/mission_registry.yaml").read_text(encoding="utf-8"))
    mode = (reg.get("controller") or {}).get("mode", "shadow")
except Exception:
    reg, mode = {"missions": {}}, "shadow"
try:
    from portfolio_automation.engineer_worker.roadmap_guard import RoadmapAuthorization
    auth = RoadmapAuthorization.read(root)
    mission = auth.authorized_mission_id if auth.authoritative else None
except Exception:
    mission = None
paused = None
try:
    ph = yaml.safe_load(pathlib.Path(".agent/phase_status.yaml").read_text(encoding="utf-8"))
    ba = ph["stockbot_northstar_redesign"]["phases"]["northstar_phase_0c"]["bounded_authorization"]
    paused = ba.get("paused_bounded_authorization")
except Exception:
    paused = None
print(json.dumps({
    "mode": mode,
    "completed_mission": mission,
    "authoritative_mission": mission,
    "registry": reg,
    "paused_authorization": paused,
    "certified_main_sha": sha,
    "protected_main_sha": sha,
    "post_merge": {"conclusion": concl, "event": event, "head_branch": branch, "head_sha": sha},
}))
PY
