# Operator Worker — Phase 1: Rootless-Container Isolation (Threat Model + Design Spec)

Date: 2026-06-18
Status: revised per security review → pending implementation plan
Milestone: hardening Phase 1 of `docs/operator_worker_hardening_spec.md`
Scope flips: `auth` gate (only after static checks AND runtime attestation). All other gates unchanged.

## Goal

Run the operator worker's `claude` invocation as a **non-root, capability-dropped,
read-only-rootfs process inside a rootless Podman container owned by a dedicated
host account**, with an isolated per-work-order git workspace and minimal
read-only credentials — and make the `auth` readiness gate reflect *effective,
attested* isolation, not configuration intent.

## Non-goals (explicitly deferred)

- **`bounded_cmd` is NOT flipped by this phase.** Containerizing `claude -p
  <prompt>` improves containment but does not create a bounded-command system.
  `bounded_cmd` stays AMBER until a separate phase implements structured action
  IDs, a closed allowlist, validated arg schemas, fixed entry points, env
  allowlisting, no-arbitrary-shell-from-WO-fields, and injection-rejection tests.
  Container isolation is cited as *partial evidence* only.
- `rollback` stays AMBER. Cost stays uncapped (Phase 2). `autonomous_enabled`
  stays false. `GUI_V2_OPERATOR_EDIT` stays false by default.
- Network egress allowlisting (deferred hardening — see Network).

## Threat Model

**Assets:** the production repository + its git refs/history (`/opt/stockbot/.git`,
`main`); host secrets (`/opt/stockbot/.env`, root `~/.ssh`, cloud creds, shell
history); the production decision artifacts (`outputs/latest/decision_plan.json`);
the host root account; Anthropic credentials.

**Trust boundary:** the worker runs an LLM (`claude`) that executes
model-directed shell/edit actions on attacker-influenceable inputs (work-order
fields, repo contents, model output). Treat everything the worker does inside
the container as **untrusted**. The control plane (dashboard, worker_runner,
readiness assessor) is trusted and runs as root.

**Attacker model:** (a) a prompt-injected or misbehaving `claude` run trying to
escape the worktree, mutate production refs, read secrets, escalate privilege,
or reach the host; (b) a malicious/crafted work-order field attempting command
or path-traversal injection into the launcher; (c) a stale/forged attestation
attempting to show the `auth` gate green without real isolation.

**Threats → mitigations:**

| Threat | Mitigation |
|---|---|
| Worker mutates `main`/prod refs/hooks/index | NEVER mount `/opt/stockbot/.git`; worker operates on an **isolated disposable clone** whose git metadata lives outside the prod repo |
| Worker reads host secrets | No `.env`, no host home mount, no broad `~/.claude`; only a dedicated minimal RO credential dir; read-only rootfs; explicit env allowlist |
| Privilege escalation inside container | `--user` dedicated non-root uid/gid; rootless Podman under `stockbot-worker`; `--security-opt=no-new-privileges`; `--cap-drop=ALL`; no privileged; no host PID/IPC ns |
| Container → host breakout via runtime socket | No docker/podman socket mounts; rootless (no daemon) |
| Resource exhaustion / fork bomb / disk fill | bounded `--pids-limit`, `--memory`, `--cpus`, `--tmpfs` size, bounded writable workspace, `--timeout`/host-side timeout |
| Command/path injection via work-order fields | argv built purely in Python with `shell=False`; image, executable, uid, and mount **sources** are constants/validated — never user-derived; work_order_id validated by strict regex before any path use |
| Forged/stale attestation shows false green | attestation must match approved image **digest**, be fresher than image build + config change, within `max_age`; missing/stale/unreadable → AMBER |
| Silent un-isolated execution | fail-closed: container-mode-enabled never falls back to direct; direct path allowed only when container mode is explicitly disabled, recorded as unisolated, `auth` stays AMBER |
| Readiness probing causes side effects | readiness/attestation read paths mutate no git refs, worktrees, or decision artifacts (tested) |

## Architecture

```
dashboard/worker_runner (root, trusted)
  └─ workspace manager: create ISOLATED disposable clone (git meta OUTSIDE prod repo)
  └─ build_container_launch_spec(...)  [pure, shell=False, all-constant sources]
  └─ validate_container_configuration(spec)  [static policy gate]
  └─ execution adapter: runuser -u stockbot-worker -- podman run <spec>
        (root drops INTO the dedicated account; no sudo grant; rootless podman
         executes under stockbot-worker; container process = non-root uid 1000)
        └─ container: read-only rootfs, cap-drop ALL, no-new-privileges
             ├─ /work       = isolated clone        (rw, bind)
             ├─ ~/.claude   = minimal creds          (ro, bind)
             ├─ writable cache/session dir           (rw, bind or tmpfs, bounded)
             ├─ /attest     = attestation output     (rw, bind, bounded)
             └─ entrypoint emits attestation (uid/gid/caps/mounts/digest/rootless)
  └─ verify_runtime_attestation(/attest/...)  → record to outputs/operator_control/
  └─ workspace manager: extract+validate diff, then DESTROY clone
readiness._auth_gate: static checks AND latest valid attestation → green/amber
```

## Dedicated host identity

- Create unprivileged account **`stockbot-worker`** (no login shell needed, **no
  sudo**), owning its rootless Podman storage (`~stockbot-worker/.local/share/containers`)
  and granted `/etc/subuid` + `/etc/subgid` ranges. (Operator-run provisioning.)
- The root control plane launches via `runuser -u stockbot-worker -- <fixed launcher
  argv>`. Root→non-root drop needs no sudo. `stockbot-worker` itself can do nothing
  privileged.
- The container runtime (podman) and all worker processes execute under
  `stockbot-worker` (rootless), mapped to container uid 1000 via its subuid range.

## `auth` gate semantics (the crux)

`auth` is GREEN only when **BOTH** hold; otherwise AMBER with the specific reason.

**A. Static capability checks** (all must pass):
- container mode explicitly enabled (`operator_control.worker_container.enabled`);
- `podman` resolved from a **fixed trusted absolute path** (configured), not `$PATH` search;
- expected image exists AND its **digest matches the approved immutable digest**
  recorded in config (a mutable tag alone never satisfies);
- launch spec specifies the dedicated non-root UID/GID;
- launch spec uses the expected rootless Podman context (`stockbot-worker`);
- spec includes `--security-opt=no-new-privileges`;
- spec includes `--cap-drop=ALL` (unless a reviewed, recorded exception);
- `--read-only` root filesystem;
- no docker/podman socket mount in the spec;
- mounts conform to the approved mount policy (see Mount policy).

**B. Runtime attestation** (a controlled smoke run or a real worker run records,
and `verify_runtime_attestation` confirms):
- execution mode was `container`;
- effective UID/GID were the expected non-root identities;
- runtime was rootless;
- `no_new_privileges` was effective;
- effective capabilities matched policy (empty set unless reviewed exception);
- actual mounts matched the approved policy;
- no sensitive host paths and no runtime sockets were present;
- the running image digest matched the approved digest.

**Verdicts:**
- No successful attestation yet → AMBER `configured but not runtime-verified`.
- Latest attempted worker execution was direct / root / uncontainerized /
  contradictory / missing required evidence → **mandatory** downgrade to AMBER.
- Attestation stale/unreadable → AMBER (fail safe).

**Freshness rule (documented):** an attestation is valid iff it (a) references the
currently-approved image digest, (b) has a timestamp ≥ the most recent of
{approved-image build time, launch-policy/config last-modified}, and (c) is no
older than `attestation_max_age` (default **30 days**). Any miss → stale → AMBER.

## Container launch baseline (`build_container_launch_spec`)

Pure argv builder, `shell=False`. Where supported, the spec includes: fixed
podman path · `--rm` · immutable image **digest** reference (`image@sha256:…`) ·
`--user <uid>:<gid>` dedicated non-root · rootless context · `--read-only` ·
`--security-opt=no-new-privileges` · `--cap-drop=ALL` · `--pids-limit` · `--memory`
· `--cpus` · bounded `--tmpfs`/scratch sizes · bounded writable workspace ·
host-side execution timeout · NO `--privileged` · NO `--pid=host`/`--ipc=host` ·
NO socket mounts · NO host-home mount · NO `.env`/secrets · explicit
`--env`/`--env-file` **allowlist** (and ANTHROPIC_API_KEY stripped). **No host
networking** (`--network=host` forbidden); default rootless egress accepted.
Image, executable, uid/gid, and all mount **sources** are constants or
config-derived — **never** from work-order fields.

## Isolated git workspace model

Production `.git` is **never** mounted (rw or ro). Per work order, a host-side
**workspace manager** creates a disposable isolated clone whose git metadata lives
outside the prod repo:
- `git clone --no-hardlinks --no-local file:///opt/stockbot <disposable-ws>` (or
  equivalent) into a dir like `/var/lib/stockbot-worker/ws/<wo_id>/` owned by
  `stockbot-worker` — self-contained `.git`, no shared objects/refs/hooks with prod.
- Mount only `<disposable-ws>` rw into the container as `/work`.
- After the run: the manager extracts the diff/patch, validates it (the existing
  protected-path guard + diff inspection), and then **destroys** the clone.
- The container cannot reach `main`, prod refs, hooks, or shared worktree metadata.

(This replaces the `git worktree add` linking-to-prod-`.git` model for the
container path. The existing scaffold/`.worktrees` path is unchanged and remains
direct/manual-only.)

## Credential model

- A dedicated `stockbot-worker`-owned credential dir containing ONLY the
  minimal file(s) `claude` requires for headless auth, mounted **read-only** at
  the container user's `~/.claude`. NOT root's `~/.claude`.
- If `claude` needs writable state (cache/session), provide a **separate bounded
  writable** dir (rw bind or sized tmpfs) — never co-located with the RO creds.
- Excluded: shell history, SSH, cloud creds, env files, any other home contents.
- `ANTHROPIC_API_KEY` continues to be stripped from the child env (necessary, not
  sufficient).
- Provisioning establishes the worker's credential dir (operator re-auths or copies
  the minimal credential file under `stockbot-worker`); documented in the runbook.

## Fail-closed behavior

When `worker_container.enabled=true`, ANY of these **fails the work order** with a
clear recorded error and **no** direct-path invocation: podman missing · image
missing or digest mismatch · rootless runtime unavailable · launch-policy
validation failure · container startup failure · runtime-attestation failure ·
invalid mount plan · credential dir unavailable · UID/GID mismatch.

Direct execution is available **only** while container mode is explicitly disabled
and the worker is scaffold/manual-only; it is recorded as `execution_mode=direct,
isolated=false`, and `auth` stays AMBER.

## Network

Default rootless egress (slirp4netns) is accepted for Phase 1 — `claude` needs
network. Documented as deferred hardening and shown honestly in the readiness
detail line (e.g. "egress: unrestricted (deferred)"). `--network=host` is forbidden.

## Components

- `operator_control/worker_container.py` (new): pure + a single narrow adapter:
  - `validate_container_configuration(cfg) -> Result` (static policy gate)
  - `build_container_launch_spec(workspace, creds, attest_dir, cfg) -> list[str]` (pure argv, shell=False)
  - `probe_container_capabilities(cfg) -> dict` (podman present at fixed path? image+digest present? rootless ok?)
  - `run_container_smoke_attestation(cfg) -> attestation` (minimal `podman run` emitting id/caps/mounts/digest; no `claude`)
  - `verify_runtime_attestation(attestation, cfg) -> Result` (matches policy + freshness)
  - the execution adapter (`runuser -u stockbot-worker -- podman run …`) is the ONLY function that actually spawns podman.
- `operator_control/worker_workspace.py` (new): isolated-clone create / validate-diff / destroy.
- `operator_control/worker_runner.py` (modify): `_invoke_claude` routes through the
  container path when enabled (fail-closed); records `execution_mode` + attestation ref.
- `portfolio_automation/operator_worker_readiness.py` (modify): rewrite `_auth_gate`
  to static-checks-AND-attestation per above; reads the latest attestation artifact;
  applies the freshness rule. (`_in_container` helper retained for the attestation
  entrypoint's self-report, recognizing `/run/.containerenv`.)
- `Containerfile` (new): `FROM python:3.12-slim` (matches host 3.12.3); `apt: git`,
  Node + `npm i -g @anthropic-ai/claude-code@2.1.x`; `pip install -r requirements.txt`
  into an image venv; create `worker` uid/gid 1000; non-root `USER`; minimal entrypoint
  that can emit attestation.
- `scripts/worker_container_setup.sh` + `docs/operator_worker_container.md` (new):
  operator-run provisioning runbook (account, subuid/subgid, linger, build, pin digest,
  enable flag, smoke-attest). Sudo/system steps are operator-run; the in-session
  classifier blocks them.
- Attestation artifact: `outputs/operator_control/worker_attestation.json` (observe-only).

## Data flow

`run()` → workspace manager creates isolated clone → `build_container_launch_spec`
→ `validate_container_configuration` (fail-closed on any miss) → adapter `runuser …
podman run` (claude inside container, writes attestation to `/attest`) → parse
claude JSON (unchanged) → `verify_runtime_attestation` → record execution_mode +
attestation → workspace manager validates diff + destroys clone. Readiness
`_auth_gate` reads the latest attestation live.

## Error handling

All capability/validation/attestation failures → recorded structured error, work
order fails, no direct fallback. Readiness functions never raise (degraded →
AMBER). Workspace manager always destroys the disposable clone (even on failure)
to avoid leak/accumulation.

## Testing

Pure/unit (no podman needed — monkeypatch the adapter):
- `build_container_launch_spec`: contains fixed podman path, `--user` non-root,
  digest reference (not bare tag), `--read-only`, `no-new-privileges`,
  `--cap-drop=ALL`, resource bounds, env allowlist, ANTHROPIC_API_KEY stripped;
  contains NO `/opt/stockbot/.git` mount, NO `.env`, NO socket mount, NO host home,
  NO `--network=host`, NO `--privileged`.
- argv contains **no user-derived** executable, image, or mount **source**;
  work_order_id path traversal cannot influence any volume path.
- `validate_container_configuration`: rejects mutable/unapproved tag (digest
  mismatch), missing socket-mount-ban, missing read-only, etc.
- enabled container mode **never** falls back to direct (each fail-closed
  condition → work order failed, direct adapter NOT called).
- `_auth_gate`: AMBER when no attestation; AMBER when attestation stale (digest
  mismatch / older than image build / older than max_age / unreadable); AMBER
  when latest execution_mode=direct or uid=root or caps/mounts mismatch; GREEN
  only when static checks AND a fresh valid attestation pass; never raises.
- `verify_runtime_attestation`: execution-mode mismatch, wrong UID/GID, capability
  mismatch, mount mismatch, socket present, host-root `~/.claude` mount present →
  each → fail (→ AMBER).
- credential dir unavailable → fail-closed; unauthorized env vars stripped.
- readiness/attestation read paths mutate no git refs, worktrees, or decision
  artifacts (guard test).
- workspace manager: isolated clone has self-contained `.git` (no shared refs with
  prod); destroy removes it even on simulated failure.

Integration/smoke (gated; only meaningful once podman is installed — guard with a
`podman`-availability skip so the suite stays green pre-provisioning):
- `run_container_smoke_attestation` against the real image yields an attestation
  whose uid≠0, rootless=true, caps empty, digest matches.

## Provisioning runbook (operator-run on prod — NOT executed until spec+plan reviewed)

Exact commands handed over: create `stockbot-worker` (no sudo, no shell);
`/etc/subuid`+`/etc/subgid` ranges; `loginctl enable-linger stockbot-worker`;
`apt-get install podman`; build image + capture its `sha256` digest; pin the
digest in config; establish the worker credential dir (re-auth/copy minimal
creds under the account); set `worker_container.enabled=true`; restart dashboard;
run `run_container_smoke_attestation` and confirm `auth` flips GREEN. Sudo/system
steps are operator-run.

## Phase outcome (acceptance)

- `auth` → GREEN **only** after static checks AND a fresh valid runtime
  attestation pass; AMBER otherwise (incl. configured-but-not-verified).
- `bounded_cmd` → remains AMBER (bounded-action layer is out of scope here).
- `rollback` → remains AMBER.
- cost → remains uncapped (Phase 2).
- `autonomous_enabled` → false. `GUI_V2_OPERATOR_EDIT` → false by default.

## Out of scope / deferred (tracked)

Bounded-action command layer (own phase); egress allowlisting; applied-change
rollback (Phase 3); cost cap (Phase 2). The CLI `cancel` transition gap remains
in the hardening spec backlog.
