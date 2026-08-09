# Agent-Lab sandbox runtime (version-controlled)

Canonical source for the security-critical R&D sandbox runtime that runs in the
`StockBot-Agent-Lab` WSL distro. Committing these files binds the certified
sandbox configuration to a git commit (Phase 0B hardening P0.6).

## Files
- `rdsbx-offline-up.sh` — creates the OFFLINE_LOCAL private netns (default-deny,
  no DNS, IPv6 off; only the inference proxy on the netns gateway is reachable).
- `ollama_inference_proxy.py` — worker-facing inference-only proxy; forwards
  `/api/generate|chat|embed|embeddings` + read-only `version|tags|ps` to local
  Ollama, refuses pull/push/create/copy/delete and all other endpoints; bounds
  request/response size.
- `systemd/rdsbx-offline.service` — brings the netns up on boot (oneshot).
- `systemd/rd-ollama-inference-proxy.service` — runs the proxy (hardened unit).
- `install.sh <src>` / `verify.sh <src>` — deploy and hash-verify.

## Deploy / update (run as root in StockBot-Agent-Lab)
```
sudo ./install.sh .        # copies to /usr/local/sbin + /etc/systemd, enables services
sudo ./verify.sh .         # installed sha256 == source sha256  -> VERIFY_OK
```
`verify.sh` binds git commit -> running boundary: a mismatch means the runtime
was changed out-of-band and must be re-inspected before certification is trusted.

## Job execution
The trusted runner (`portfolio_automation/rd_control/sandbox.py`, Agent-Lab
backend) launches each worker as a transient systemd **service** — `systemd-run
--wait --pipe` with: `User=rd-worker`, `NetworkNamespacePath=/run/netns/rdsbx-offline`,
`ProtectHome`, `PrivateTmp`, `ProtectSystem=strict`, `TemporaryFileSystem=<jobs root>`
(masks sibling jobs), `BindReadOnlyPaths=<job input>`, `BindPaths=<job workspace/output>`,
`NoNewPrivileges=yes`, `TasksMax`, `MemoryMax`, `CPUQuota`, and an explicit
`--setenv` allowlist. On timeout and on operator cancel the runner
(`run_job`/`cancel_job` with `contain_via_unit=True`) `systemctl kill -s
SIGKILL`s the transient unit's whole cgroup and **verifies it is empty/dead
before** transitioning to `TIMED_OUT`/`CANCELLED` (setsid/double-fork and
`SIGTERM`-ignoring descendants cannot escape); it fails closed to
`FAILED_SANDBOX` if the cgroup cannot be proven cleared. Killing the
`systemd-run` client alone is not treated as worker termination.

## Not version-controlled here
Secrets, machine credentials, Prime session state, the SQLite registry, and the
`rd-worker`/`stockbot-agent` accounts (created out-of-band).
