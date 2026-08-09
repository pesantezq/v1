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

**Private scratch (`/tmp`, `/dev/shm`):** `PrivateTmp=yes` gives each job its own
`/tmp` + `/var/tmp`. A second per-service tmpfs is mounted over **`/dev/shm`**
(`TemporaryFileSystem=/dev/shm:rw,nosuid,nodev,size=64M,mode=1777`) so `/dev/shm` is **not**
the shared host tmpfs — no cross-job scratch, no cross-job persistence, and an
**explicit 64 MiB size cap** (`nosuid`,`nodev`). This size cap — not `MemoryMax`
— is the authoritative `/dev/shm` bound: shared-tmpfs pages are not charged to
the job's memory cgroup, so without this a job could write far beyond `MemoryMax`
into a host-shared `/dev/shm`. (Verified: pre-fix a later job read an earlier
job's `/dev/shm` marker and a 256 MiB write survived `MemoryMax=64M`; post-fix
both are denied.)
Denied to the worker: the registry DB, other jobs, system config, network-policy
files, the human home, Windows drives, and — via the dedicated unprivileged
`rd-worker` user — the Prime runtime user's credentials.

## Process containment (cgroup) — including timeout & cancel
The worker runs as a transient systemd **service** `rdsbx-job-<job_id>` with
`KillMode=control-group`, so a worker that `setsid`/double-forks (even one that
ignores `SIGTERM`) cannot escape cgroup membership. Three termination paths are
enforced by the trusted runner, all targeting the **service cgroup**, not the
`systemd-run` client:

* **Normal exit** — systemd reaps the whole cgroup when the main process exits.
* **Timeout** — the runner (`run_job(contain_via_unit=True)`) SIGKILLs the
  service cgroup (`systemctl kill --signal=SIGKILL rdsbx-job-<job_id>.service`),
  then **verifies** the cgroup is inactive with zero PIDs within a bounded
  interval **before** transitioning to `TIMED_OUT`. If the boundary cannot be
  proven empty it **fails closed** to `FAILED_SANDBOX` — the runner never
  reports `TIMED_OUT` with survivors.
* **Cancel** — `cancel_job(contain_via_unit=True)` performs the same
  cgroup-SIGKILL + bounded verification before transitioning to `CANCELLED`,
  and likewise fails closed to `FAILED_SANDBOX` if survivors remain.

The unit name is derived from the job id through a strict allowlist
(`systemd_unit_for`) and passed to `systemctl` as an argv element (never a
shell), so a hostile job id can neither inject shell syntax nor be parsed as a
`systemctl` option. Killing the `systemd-run` client alone is **not** treated as
worker termination. `--collect` removes the transient unit afterward.

Verified live (adversarial worker: fork → `setsid` → double-fork →
`SIGTERM`-ignoring daemon): both timeout and cancel end with the daemon dead and
`cgroup_pids=0`, cleanup latency ~0.7 s; normal success is unaffected. (An
optional independent `RuntimeMaxSec`, env `RDSBX_RUNTIME_MAX_SEC`, provides a
second systemd-enforced timeout.)

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
