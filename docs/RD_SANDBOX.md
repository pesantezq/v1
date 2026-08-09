# R&D Sandbox Runner — Phase 0B (hardened)

Framework-neutral execution boundary for future StockBot workers. The trusted
R&D control plane materializes a bounded job, runs an **untrusted** worker inside
a technically-enforced sandbox, captures a job-local result, validates it
deterministically, and drives the Phase 0A registry lifecycle via CAS. Additive;
no production behaviour change; built alongside Prime (Prime not removed).

This document reflects the **Phase 0B Hardening Closure**: the sandbox is now a
systemd transient **service** (not a bare `runuser`), so mount isolation, cgroup
process containment, resource caps, capability drop, and a clean environment are
enforced by the kernel — not by trusting the worker.

## Threat model
Assume the worker is actively hostile: it will try to read another job, escape
its process tree, reach the internet, manage the model, escalate privilege, read
the runner's environment, and hand back a booby-trapped result file. Nothing
below relies on prompts, model goodwill, expected paths, or exit code 0. Every
control is enforced technically and verified adversarially.

## Two layers (and where each is proven)
1. **Trusted runner** — `portfolio_automation/rd_control/sandbox.py` (repo, human
   distro). Materialization, immutable input manifest, result envelope + strict
   validator, **hostile-safe result ingestion**, deterministic lifecycle via
   Phase 0A CAS, timeout + process-group kill, cancellation, output bounds,
   restart reconciliation, and a **clean-by-construction** worker environment.
   Proven by 58 hermetic tests (`tests/test_rd_sandbox.py`) with fake worker
   subprocesses (`jail_wrapper=()`), so the contract is provable anywhere.
2. **OS isolation** — Agent-Lab (`StockBot-Agent-Lab` distro). The worker runs
   via `sandbox-run`, which launches it as a systemd transient service that
   enforces per-job mount isolation, cgroup containment, resource caps, least
   privilege, and the `rdsbx-offline` offline network namespace. Proven by a live
   10-test adversarial certification in the real environment (below).

The runner is OS-isolation-agnostic: `jail_wrapper` is a prefix
(`["/usr/local/sbin/sandbox-run", "rdsbx-offline", "--"]` in Agent-Lab; empty in
hermetic tests), so the trusted contract and the OS boundary are testable
independently.

## Version-controlled runtime (hash-bound boundary)
The entire OS boundary lives in `ops/agent_lab/` under version control:
`sandbox-run.sh`, `rdsbx-offline-up.sh`, `ollama_inference_proxy.py`, the two
systemd unit files, `install.sh`, and `verify.sh`. `install.sh` deploys them and
prints installed `sha256`; `verify.sh` recomputes and compares source→installed,
emitting `VERIFY_OK` only when the running boundary is byte-identical to the
committed source. This binds the git commit to the enforced boundary and survives
`wsl --terminate` (verified).

## Filesystem contract (per-job, kernel-enforced)
```
<jobs_root>/<job_id>/
  input/      read-only to worker (0555 root-owned; holds manifest.json)
  workspace/  read/write, job-scoped (chowned to rd-worker)
  output/     read/write, job-scoped (worker writes result.json here)
  .runner/    runner-private (pidfile); NOT exposed to the worker
```
`sandbox-run` mounts an **empty tmpfs over the entire jobs root**
(`TemporaryFileSystem=`), then binds back **only this job's** `workspace/` +
`output/` (writable) and `input/` (read-only). `ProtectSystem=strict` makes the
rest of the host tree read-only; `ProtectHome=yes`, `PrivateTmp=yes`. Result:
every sibling job is invisible and unreachable, and the host is unwritable.
Denied to the worker: the registry DB, other jobs, system config, network-policy
files, the human home, Windows drives, and — via the dedicated unprivileged
`rd-worker` user — the Prime runtime user's credentials.

## Process containment (cgroup)
The worker is a systemd service cgroup with `KillMode=control-group`. A worker
that `setsid`/double-forks a daemon cannot escape cgroup membership, so the
descendant is reaped when the service stops or is killed — verified live (a
`setsid` daemon was dead the instant the service exited) and by `systemctl kill`
on the whole unit. `--collect` removes the transient unit afterward.

## Resource caps
`TasksMax=64`, `MemoryMax=2G`, `CPUQuota=200%`, plus an optional independent
`RuntimeMaxSec` (env `RDSBX_RUNTIME_MAX_SEC`) enforced by systemd alongside the
runner's own `timeout_seconds`. Verified: a thread-bomb worker stalled at 63
threads (TasksMax bit), and a 600 s sleeper was killed at ~3 s by `RuntimeMaxSec`.

## Least privilege
`User=rd-worker` (uid 1001, no extra groups), `NoNewPrivileges=yes`, empty
`CapabilityBoundingSet`/`AmbientCapabilities`, `RestrictSUIDSGID`,
`LockPersonality`, `ProtectKernelTunables/Modules/ControlGroups`. Verified live:
`NoNewPrivs=1`, `CapEff=0000000000000000`, `CapBnd=0000000000000000`.

## Environment sanitization
The runner builds the worker environment **from scratch** — it never inherits
`os.environ`, and the systemd service is given only an explicit `--setenv`
allowlist (`RD_*`, `PATH`, `HOME`, `RD_OLLAMA_URL`). Verified live: fake secrets
exported into the runner's own environment (`FAKE_LIVE_SECRET`,
`FAKE_AWS_SECRET_ACCESS_KEY`) did **not** appear in the worker; no secret-shaped
key leaked.

## OFFLINE_LOCAL network contract (inference-only)
Default-deny egress in the `rdsbx-offline` netns (`table inet sbxfw`, policy
`drop`, scoped **inside the netns** — it does not touch the main namespace or
Windows). The ONLY reachable destination is the **inference-only Ollama proxy**
at the netns gateway `10.201.0.1:11435` (fixed IP). **No DNS** (empty
`resolv.conf`, no `:53` rule), no internet, no LAN, no host services. IPv6 is
disabled in the netns and the `inet` filter covers v6 as well.

The proxy (`rd-ollama-inference-proxy`) forwards **only** inference/read
endpoints to local Ollama and returns **403** for model management:
- ALLOWED: `POST /api/generate|chat|embed|embeddings`, `GET /api/version|tags|ps`.
- DENIED (403): `pull`, `push`, `create`, `copy`, `delete`.

This forwards straight to `127.0.0.1:11434` and is **independent of any Prime-era
bridge** (no `socat`, no `tinyproxy`, no `agentjail`).

## Hostile result-file ingestion
`output/` is worker-controlled, so `result.json` is hostile input. The trusted
reader (`_read_result_safe`) opens with `O_NOFOLLOW|O_NONBLOCK`, requires a
**regular file** (`S_ISREG`), and reads at most `max_output_bytes` — so a symlink
(e.g. to `/etc/passwd`), FIFO, device, directory, or oversize/growing file cannot
turn the reader into a confused deputy. Verified live (symlinked `result.json` →
`FAILED_VALIDATION`) and by hermetic tests (symlink/FIFO/dir/oversize).

## Process lifecycle (Phase 0A states; validator decides success)
`QUEUED → ADMITTED → RUNNING → RESULT_RECEIVED → VALIDATING → SUCCEEDED |
FAILED_VALIDATION`. Failure mapping: worker non-zero → `FAILED_WORKER`; sandbox
setup/exec failure → `FAILED_SANDBOX`; timeout → `TIMED_OUT`; operator cancel →
`CANCELLED`; orphaned RUNNING after restart → `INTERRUPTED` (Phase 0A stale
reconciliation). **Exit 0 does NOT imply SUCCEEDED** — the trusted validator
does. All transitions use the Phase 0A CAS path; a failed transition writes no
phantom audit.

## Result validation
The worker result is DATA, not an instruction, and its self-declared status is
ignored. Rejected (→ FAILED_VALIDATION) on: oversize, invalid JSON/encoding,
schema mismatch, `job_id` mismatch, input-manifest-hash mismatch, payload-hash
mismatch, path-traversal/out-of-scope markers, missing required fields, an
attempt to declare an authoritative status, or a non-regular/symlinked result
file.

## One-command certification
`ops/agent_lab` + the cert harness run end to end: deploy → `VERIFY_OK` → stop
Prime egress services → run 10 adversarial jobs through the real runner → restore
Prime. The live suite (with Prime **stopped**) passed 10/10: E2E real inference,
cross-job read denied, setsid-daemon reaped, TasksMax enforced, `NoNewPrivs`/caps,
env sanitized, Ollama inference-only (mgmt 403), no egress (v4/v6/raw-ollama),
hostile symlink rejected, systemd runtime timeout.

## Recovery
Runner/worker death, WSL shutdown, GamingMode, reboot, partial result: a job left
RUNNING is reconciled to INTERRUPTED by the Phase 0A stale check on the next
control-plane pass (stale-job reconciliation, **not** a lease/heartbeat system).
No automatic retries; retry = a new job. The netns + proxy are enabled systemd
services that auto-restore on boot (verified after `wsl --terminate`).

## Cross-distro note (known limitation / qualification)
The trusted runner code currently lives in the human distro repo; the enforced
OS boundary lives in Agent-Lab. Phase 0B certifies the trusted contract
(hermetic) and the OS boundary (live) plus a live end-to-end that imports the
runner into Agent-Lab and runs real jailed jobs. Deploying the runner *into*
Agent-Lab as the standing operating control plane is a Phase 0C/1A step.

## Security claims — precise
This is a **Linux/WSL systemd-service sandbox**: per-job mount namespace
isolation, cgroup process/resource containment, capability drop +
`NoNewPrivileges`, an unprivileged user, a clean environment, an offline network
namespace with an inference-only egress proxy, and hostile-safe result ingestion
— all verified adversarially. It is **not** claimed to be a full container
runtime or VM-equivalent (shared kernel; no seccomp profile or user-namespace
remap is applied). Within that scope, an intentionally malicious local worker
could not, in testing, escape the filesystem, process, resource, privilege,
environment, or network confines of its approved job.
