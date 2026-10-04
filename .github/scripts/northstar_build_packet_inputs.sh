#!/bin/bash
# Emit NORTHSTAR dispatch-packet inputs JSON (stdout) from TRUSTED main state.
# Args: <certified_sha> [requested_mission]
# IO glue. Authority + prompt come only from protected state; requested_mission is
# passed through for the packet builder to REJECT if it does not match (never override).
set -uo pipefail
SHA="${1:?certified sha}"; REQ="${2:-}"
python - "$SHA" "$REQ" <<'PY'
import json, sys, pathlib
sha, req = sys.argv[1], (sys.argv[2] or None)
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
prompt_text = None
entry = (reg.get("missions") or {}).get(mission or "", {})
src = entry.get("prompt_source")
if src and pathlib.Path(src).is_file():
    prompt_text = pathlib.Path(src).read_text(encoding="utf-8")
print(json.dumps({
    "mode": mode,
    "authoritative_mission": mission,
    "requested_mission": req,
    "registry": reg,
    "certified_main_sha": sha,
    "protected_main_sha": sha,
    "prompt_text": prompt_text,
}))
PY
