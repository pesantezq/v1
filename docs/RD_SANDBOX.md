# R&D Sandbox Runner — Phase 0B

Framework-neutral execution boundary for future StockBot workers. The trusted
R&D control plane materializes a bounded job, runs an **untrusted** worker,
captures a job-local result, validates it deterministically, and drives the
Phase 0A registry lifecycle via CAS. Additive; no production behaviour change;
built alongside Prime (Prime not removed).

## Trust boundary
The control plane is trusted; the worker is untrusted. A worker result is DATA,
not an instruction. A worker can never: write `data/rd_control.db`, set its own
authoritative status, modify another job or the StockBot shadow, escalate
privilege, reconfigure the sandbox network, or reach the internet. Authority
lives in deterministic code; the worker's self-declared status is ignored (and,
if it names a reserved JobStatus, the envelope is rejected).

## Two layers (and where each is proven)
1. **Trusted runner** — `portfolio_automation/rd_control/sandbox.py` (repo, human
   distro). Materialization, immutable input manifest, result envelope + strict
   validator, deterministic lifecycle via Phase 0A CAS, timeout + process-group
   kill, cancellation, output bounds, restart reconciliation. Proven by 19
   hermetic tests (`tests/test_rd_sandbox.py`) with fake worker subprocesses
   (`jail_wrapper=()`), so the contract is provable anywhere.
2. **OS isolation** — Agent-Lab: the `rdsbx-offline` network namespace + a
   dedicated `rd-worker` user. Proven by live adversarial certification in the
   real namespace (below). The runner is OS-isolation-agnostic: `jail_wrapper`
   is the netns/`runuser` prefix (empty in tests; `ip netns exec rdsbx-offline
   runuser -u rd-worker --` in Agent-Lab via `sandbox-run`).

**Cross-distro note (known limitation):** the trusted runner code currently lives
in the human distro repo; the jail + Ollama live in Agent-Lab. Phase 0B certifies
the two halves independently plus a live OS-level end-to-end. Deploying the
runner *into* Agent-Lab as the operating control plane is a Phase 0C/1A step.

## Filesystem contract
```
job/
  input/      read-only to worker (0555 root-owned; holds manifest.json)
  workspace/  read/write, job-scoped (owned by rd-worker)
  output/     read/write, job-scoped (worker writes result.json here)
  .runner/    runner-private (pidfile); NOT given to the worker
```
Denied to the worker: registry DB, StockBot shadow (write), other jobs, system
config, network-policy files, human home, Windows drives, and — via the dedicated
`rd-worker` user — the Prime runtime user's credentials.

## OFFLINE_LOCAL network contract (certified)
Default-deny egress in the `rdsbx-offline` netns; the ONLY allowed destination is
the local Ollama bridge `10.200.0.1:11434` (by fixed IP). **No DNS**, no proxy,
no internet, no LAN, no host services. resolv.conf in the netns is empty.
- **DNS hardening:** there are no `:53` allow rules at all and no resolver
  configured, so arbitrary/encoded DNS lookups and direct UDP/TCP 53 (to the WSL
  resolver or any public resolver) are dropped. The Ollama path needs no name
  resolution (fixed IP), so closing DNS costs nothing. This closes the DNS-exfil
  channel that the earlier Prime egress jail left open.

## Certified (live, in the real netns as an unprivileged user)
ALLOWED: Ollama by IP (`/api/version` → 0.32.6).
BLOCKED: arbitrary DNS (A/encoded/UDP53/TCP53 to resolver and 8.8.8.8), arbitrary
HTTPS/HTTP/TCP, SSH:22, private LAN, host gateway, other ports on the bridge host,
python raw socket. PRIVILEGE: no sudo, `CapEff=0`, `nft`/`ip netns`/`unshare
--net`/`mount` all denied → worker cannot reconfigure its confinement. FS: shadow
write denied, `/mnt/c` absent. CREDENTIALS: `rd-worker` cannot read the Prime
user's `auth.json`.

## Process lifecycle (Phase 0A states; validator decides success)
`QUEUED → ADMITTED → RUNNING → RESULT_RECEIVED → VALIDATING → SUCCEEDED |
FAILED_VALIDATION`. Failure mapping: worker non-zero → `FAILED_WORKER`; sandbox
setup/exec failure → `FAILED_SANDBOX`; timeout → `TIMED_OUT`; operator cancel →
`CANCELLED`; orphaned RUNNING after restart → `INTERRUPTED` (Phase 0A stale
reconciliation). **Exit 0 does NOT imply SUCCEEDED** — the trusted validator does.
All transitions use the Phase 0A CAS path; a failed transition writes no phantom
audit.

## Result validation
Rejected (→ FAILED_VALIDATION) on: oversize (> max_output_bytes), invalid
JSON/encoding, schema mismatch, job_id mismatch, input-manifest-hash mismatch,
payload-hash mismatch, path-traversal/out-of-scope markers, missing required
fields, or an attempt to declare an authoritative status.

## Timeout / cancellation
Workers run in their own session (`start_new_session`); timeout and cancel
`SIGTERM`→`SIGKILL` the whole process **group**, so grandchildren do not survive
(tested with a child `sleep`).

## Recovery
Runner/worker death, WSL shutdown, GamingMode, reboot, partial result: a job left
RUNNING is reconciled to INTERRUPTED by the Phase 0A stale check on the next
control-plane pass (this is stale-job reconciliation, **not** a lease/heartbeat
system). No automatic retries; retry = a new job.

## Resource / cleanup
`timeout_seconds`, `max_output_bytes`, and bounded stdout/stderr capture cap a
job. Job dirs live under a jobs root; the caller removes them after terminal
state (tests use temp dirs). No scheduler is built.

## Persistence & wrappers (Agent-Lab)
`rdsbx-offline.service` (systemd oneshot, enabled) recreates the netns on boot;
`sandbox-run` (root → drops to rd-worker in the netns) is the OS-exec entry;
`sandbox-status` is read-only. Verified: after `wsl --terminate` the netns
auto-restores with policy `drop`; human Ubuntu networking is unaffected;
GamingMode still stops both distros and releases the VM.

## Security claims — precise
This is **the implemented Linux/WSL network-namespace + unprivileged-user +
filesystem-permission isolation boundary**. It is **not** claimed to be
container-equivalent, VM-equivalent, or secure against all exfiltration. What is
demonstrated: default-deny egress with only a fixed local Ollama IP allowed, DNS
egress closed, privilege escalation denied, sandbox-network reconfiguration
denied, and filesystem/credential scoping — all verified live in the namespace.
