# northstar_continuous_mission_orchestration_foundation

This is the protected implementation contract for the Northstar continuous mission orchestration foundation.

## Objective

Make GitHub own deterministic waiting and gate evaluation between exact-head CI, exact-head Codex review, SHA-guarded merge, post-merge main certification, a separately reviewed protected-state transition, and handoff to the next mission only when that mission is already authorized.

Claude, the Engineer Worker, and implementation code must never gain merge authority or mission self-authorization.

## Required invariants

- Read .agent/project_state.yaml, .agent/phase_status.yaml, .agent/mission_registry.yaml, and config/ew0a_runtime.json before work.
- All dispatch mirrors must identify this exact mission.
- GitHub controller code runs only from trusted main, never from PR-head code.
- CI evidence is exact-head and all required checks must be green.
- Codex findings are bound using original_commit_id; inherited findings from older heads do not become fresh findings.
- Merge uses an expected-head SHA and refuses drift.
- config/ew0a_runtime.json:auto_merge remains false. Merge authority belongs only to the trusted GitHub controller.
- Automatic state changes happen only through a normal governance PR with CI + Codex review.
- Automatic transition edges must be explicitly marked preauthorized_auto in .agent/mission_registry.yaml.
- human_required transitions stop and surface an approval boundary.
- E4, real-evidence access, production, broker, capital/trading, C1, and authority-promotion transitions remain human-required unless separately authorized by the operator.
- Claude Code Action may implement only the mission named by protected state.
- The orchestration controller and registry are protected surfaces for ordinary future missions.

## Delivery

Implement the protected mission registry, GitHub controller workflows, deterministic controller/transition scripts, focused tests, and the universal orchestration contract in the Claude feature template/repo instructions.

Do not merge your own PR. The controller becomes active only after this mission is merged and post-merge main CI is green.
