# Operator Worker Container — Provisioning Runbook

This document covers every step required to provision the `stockbot-worker` rootless Podman
container that isolates the autonomous operator worker from the production VPS filesystem.

---

## Overview

The operator worker runs short-lived advisory/repair tasks inside a rootless Podman container
under a dedicated non-root system account (`stockbot-worker`, uid 2000). The container is:

- **Read-only filesystem** with three explicit volume mounts only.
- **No capabilities** (`--cap-drop=ALL`), no new privileges.
- **No host-home, no docker socket** mounted inside.
- Credentials injected read-only; attestation written to a dedicated volume.

The `auth` readiness gate flips **GREEN** only after all static checks pass, capabilities are
confirmed, and a fresh runtime attestation is present.

Deferred: network egress is currently unrestricted inside the container. A firewall rule or
Podman network policy to restrict outbound connections to known endpoints is a planned
follow-on hardening step.

---

## Prerequisites

| Requirement | Notes |
|---|---|
| Ubuntu 22.04 / 24.04 LTS on the VPS | Other Debian-like distros likely work; untested. |
| `podman` ≥ 4.0 | Installed via `apt-get` (see step 2). |
| Running as a user with `sudo` | All system-level steps require sudo. |
| `/opt/stockbot` is the project root | Adjust paths if different. |

---

## Provisioning Sequence

Run the guided setup script for each step. The script **prints** system-mutating commands
for you to review and run manually — it never runs sudo commands on your behalf.

```bash
cd /opt/stockbot
```

### Step 1 — Check current state

```bash
bash scripts/worker_container_setup.sh check
```

Inspects whether podman is installed, the `stockbot-worker` account exists, `/etc/subuid`
and `/etc/subgid` entries are present, and linger is enabled. Safe to run at any time.

---

### Step 2 — Install Podman  _(operator-run: sudo)_

```bash
bash scripts/worker_container_setup.sh install
```

Prints:

```bash
sudo apt-get update && sudo apt-get install -y podman
```

Run that command, then verify:

```bash
podman --version
podman info --format '{{.Host.Security.Rootless}}'  # expected: true
```

---

### Step 3 — Create the `stockbot-worker` system account  _(operator-run: sudo)_

```bash
bash scripts/worker_container_setup.sh account
```

Prints:

```bash
sudo groupadd --gid 2000 stockbot-worker          # if the group doesn't exist yet
sudo useradd --system --uid 2000 --gid 2000 \
    --no-create-home --shell /usr/sbin/nologin stockbot-worker
```

Verify:

```bash
id stockbot-worker
```

---

### Step 4 — Add `/etc/subuid` + `/etc/subgid` ranges  _(operator-run: sudo)_

```bash
bash scripts/worker_container_setup.sh subid
```

Prints:

```bash
echo 'stockbot-worker:2000:65536' | sudo tee -a /etc/subuid
echo 'stockbot-worker:2000:65536' | sudo tee -a /etc/subgid
```

These ranges allow the `stockbot-worker` account to run rootless Podman containers
with a full UID/GID namespace.

Verify:

```bash
grep stockbot-worker /etc/subuid /etc/subgid
```

---

### Step 5 — Enable `loginctl linger`  _(operator-run: sudo)_

```bash
bash scripts/worker_container_setup.sh linger
```

Prints:

```bash
sudo loginctl enable-linger stockbot-worker
```

Linger keeps the user's systemd session alive so rootless Podman can manage its own cgroups
without requiring an active login session.

Verify:

```bash
loginctl show-user stockbot-worker | grep Linger   # expected: Linger=yes
```

---

### Step 6 — Build the container image

```bash
bash scripts/worker_container_setup.sh build
```

Runs:

```bash
podman build -t localhost/stockbot-worker -f docker/Containerfile .
```

This executes as your current user (rootless). The first build takes several minutes
(downloads `python:3.12-slim`, installs `claude-code`).

---

### Step 7 — Capture the sha256 digest

```bash
bash scripts/worker_container_setup.sh digest
```

Outputs the pinned digest, e.g.:

```
sha256:abc123…
image_build_ts (epoch): 1750000000
```

Copy **both values** — you'll need them in step 8.

Manual alternative if the subcommand fails:

```bash
podman image inspect --format '{{.Digest}}' localhost/stockbot-worker
date +%s   # for the epoch timestamp
```

---

### Step 8 — Pin the digest in `config.json`

```bash
bash scripts/worker_container_setup.sh pin
```

Prints the JSON fragment to add under `operator_control` in `config.json`:

```json
"worker_container": {
  "enabled": false,
  "podman_path": "/usr/bin/podman",
  "image_ref": "localhost/stockbot-worker",
  "image_digest": "sha256:<PASTE_DIGEST_HERE>",
  "image_build_ts": <PASTE_EPOCH_HERE>,
  "run_as_user": "stockbot-worker",
  "container_uid": 2000,
  "container_gid": 2000,
  "attestation_path": "outputs/operator_control/worker_attestation.json",
  "attestation_max_age_days": 30,
  "env_allowlist": ["OPENAI_API_KEY", "FMP_API_KEY"],
  "cap_drop_exceptions": [],
  "resource_limits": {
    "pids": 256,
    "memory": "2g",
    "cpus": "1.0",
    "tmpfs_size": "512m",
    "timeout_seconds": 600
  }
}
```

**Keep `enabled: false` at this stage.** Enable it only after the smoke attestation
passes and `auth` flips GREEN.

**Important:** `image_digest` must be a pinned `sha256:…` string (mutable tags are
rejected by static validation). `ANTHROPIC_API_KEY` must NOT appear in `env_allowlist`
(the container uses `~/.claude` OAuth credentials, not an API key).

---

### Step 9 — Establish the worker credential directory  _(operator-run: sudo)_

```bash
bash scripts/worker_container_setup.sh creddir
```

Prints:

```bash
sudo mkdir -p /opt/stockbot-worker-creds
sudo chown stockbot-worker:stockbot-worker /opt/stockbot-worker-creds
sudo chmod 0700 /opt/stockbot-worker-creds
```

Then, to populate the credentials:

```bash
sudo -u stockbot-worker XDG_CONFIG_HOME=/opt/stockbot-worker-creds claude --version
```

The resulting `/opt/stockbot-worker-creds/.claude` directory is the read-only
`creds_dir` mount inside the container.

---

### Step 10 — Smoke attestation

```bash
bash scripts/worker_container_setup.sh attest
```

Runs the `worker_attest.sh` script inside the container and writes
`outputs/operator_control/worker_attestation.json`. Confirm the output contains:

```json
{
  "execution_mode": "container",
  "uid": 2000,
  "gid": 2000,
  "rootless": true,
  "no_new_privileges": true,
  "effective_caps": [],
  "socket_mounts_present": false,
  "host_home_mounted": false
}
```

---

### Step 11 — Enable the container

Edit `config.json` and set:

```json
"worker_container": {
  "enabled": true,
  ...
}
```

---

### Step 12 — Verify `auth` flips GREEN

```bash
python -c "
from portfolio_automation.operator_worker_readiness import operator_worker_readiness
import json
print(json.dumps(operator_worker_readiness('.'), indent=2))
"
```

Expected result: `gates.auth.status == "green"`.

If `auth` is AMBER, the reason field explains which check failed. Common causes:

| Reason | Fix |
|---|---|
| `container mode disabled` | Set `enabled: true` in config.json |
| `static checks failed: image_digest must be a pinned sha256…` | Run step 7–8 again |
| `capability probe failed` | Podman not installed / image not built |
| `configured but not runtime-verified (no attestation)` | Run step 10 (attest) |
| `attestation stale` | Re-run step 10 after any config.json change |
| `image digest mismatch vs approved` | Rebuild + re-pin the digest |

---

## Kill-Switch

To disable the container layer immediately without editing `config.json`:

```bash
touch config/operator_worker.DISABLED
```

The `autonomous_enabled()` function in `worker_runner.py` checks for this file before
any autonomous dispatch. Removing the file re-enables (subject to config).

You can also set `worker_container.enabled: false` in `config.json` directly — this
causes `auth` to return AMBER and prevents container dispatch.

---

## Updating the Image

After any change to `docker/Containerfile` or project dependencies:

1. `bash scripts/worker_container_setup.sh build` — rebuild.
2. `bash scripts/worker_container_setup.sh digest` — capture new digest.
3. Update `config.json`: `image_digest` and `image_build_ts`.
4. `bash scripts/worker_container_setup.sh attest` — refresh attestation.
5. Verify `auth` is still GREEN.

---

## Deferred: Network Egress

The container currently has unrestricted outbound network access. A future hardening step
will add a Podman network policy (or host-level iptables rule) that allows only:

- `api.anthropic.com` (Claude API)
- `financialmodelingprep.com` (FMP API)
- `api.openai.com` (OpenAI API)

Until that step ships, the `auth` gate reason line reads:
`"egress: unrestricted — deferred"`.

This is tracked as a known limitation and does not block Phase 1 activation.
