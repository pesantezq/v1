# Mission: northstar_continuous_mission_orchestration_foundation

- **Executor:** claude
- **Risk class:** E3
- **Authority source (authoritative):** `.agent/project_state.yaml`,
  `.agent/phase_status.yaml`, `config/ew0a_runtime.json`, `config/agent_policy.yaml`
  on base `main`. This prompt EXPLAINS the mission; it never grants authority.

## Objective
Build the repo-native continuous mission orchestration foundation that removes
manual waiting between: Claude implementation → exact-head Northstar CI →
exact-head Codex review → zero unresolved material findings → deterministic merge
gate → merge → exact-main-SHA post-merge CI → deterministic protected-state
transition proposal → next already-authorized mission → Claude handoff — without
weakening Northstar's existing authority model. GitHub owns waiting and
deterministic state evaluation; Claude implements only authorized work.

## Hard boundaries (fail closed)
Claude may NOT: authorize itself, select an unauthorized next mission, merge its
own PR, directly mutate protected governance state on `main`, or grant
production / broker / capital / trading / C1 / Phase-0D / real-evidence
authority. Human/operator authority is final. The implementation ships in
`shadow` mode and must not switch itself to `enabled`.

## Completion
Durable on `main` via the ordinary governance process (operator merge) with
post-merge main CI green on the exact merge SHA. Completion/resume state is
PROPOSED (not self-applied) after this implementation becomes durable.

## On success
Transition policy `preauthorized_auto`: propose restoring the already-authorized
`northstar_vs002_execution_adapter_foundation` by restoring the EXACT
`phase_status.yaml` `paused_bounded_authorization` object (never widened).
