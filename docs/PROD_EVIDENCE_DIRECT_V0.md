# Temporary Direct Production Evidence Bridge V0

> **UPDATE (permanent key + certified VPS account).** The VPS side is certified
> as `STOCKBOT_ENGINEER_RESTRICTED_ACCOUNT_READY`: the account is
> **`stockbot-engineer`** with forced command
> **`/usr/local/sbin/stockbot-engineer-read`** (root-owned) and the certified
> restricted `authorized_keys` template. This supersedes the earlier
> `stockbot-observer`/`stockbot-observe` naming in the bootstrap packet below
> (kept as the reference template). The permanent trusted-controller key and its
> enrollment/host-key-pin/smoke steps are in the **"Permanent controller key"**
> section at the end of this document. The local collector
> (`prod_evidence.py`) now targets `stockbot-engineer`, exposes model-visible
> `ModelCapability` (`PROD_*`) names mapped to the certified server verbs, fails
> **closed** on secret-like content, verifies **run identity**, and writes an
> immutable, no-overwrite, symlink-safe local snapshot.

**Status: TEMPORARY (`temporary_direct_read`) / experimental / non-canonical.**
Lets the **trusted local Engineer Worker controller** retrieve a narrow set of
**read-only** evidence from the StockBot production VPS so the local Engineer
Worker can diagnose the *actual* current Daily Safe run. **Replaced by** the
governed Agent Export / authenticated evidence-admission path — do not let
Finance Worker or any future consumer inherit this capability; every additional
consumer needs explicit authority review.

## Authority model (unchanged boundaries)
```
Engineer Worker (untrusted, sandboxed, no key, no route, default-deny egress)
    │  requests capability READ_PRODUCTION_DAILY_EVIDENCE (enum + validated selector)
    ▼
Trusted LOCAL controller  (holds the observer key; runs fixed-argv ssh)
    │  ssh -i ~/.ssh/stockbot_observer  stockbot-observer@<vps>  <cap> [selector]
    ▼
VPS: dedicated `stockbot-observer` account  (no sudo, key-only, ForceCommand)
    │  /usr/local/sbin/stockbot-observe  — enum verbs over an allowlist only
    ▼
Approved production evidence (logs / manifest / status / health / fixed SELECTs)
    ▼
Trusted local admission: size bound → secret scan/sanitize → run_id/commit bind
    → SHA-256 → immutable local snapshot → ProductionEvidenceDirectV0
    ▼
Engineer Worker receives ONLY the admitted, sanitized view (never raw stdout,
never host/key/identity).
```
The **worker never SSHes, never holds the key, never addresses production.** Only
the trusted controller does, through one restricted capability.

### The observer key
Lives ONLY in the trusted human Ubuntu environment at `~/.ssh/stockbot_observer`.
It must never be copied into: StockBot-Agent-Lab, rd-worker, the Engineer Worker
sandbox, the repository, `.env`, model input, or Windows shared storage. The
existing Windows/admin key stays the human operator path and is **not** reused.

## Local half (implemented + tested — `portfolio_automation/engineer_worker/prod_evidence.py`)
* `ProductionEvidenceCapability` (server verbs): `daily-status`, `daily-log`,
  `run-manifest`, `artifact`, `db-query`. `db-query` selects a **fixed** SQL by
  ID (`latest-daily-run`, `recent-health`, `recent-errors`) — SQL text is never
  client-supplied.
* `validate_selector` — per-capability allowlist; no path, no traversal, no SQL,
  no shell, no control chars. Invalid selectors are rejected **before** any SSH.
* `ProductionEvidenceCollector` — fixed-argv ssh (never `shell=True`) with
  `BatchMode=yes`, `IdentitiesOnly=yes`, `StrictHostKeyChecking=yes`, a dedicated
  `UserKnownHostsFile`, `ClearAllForwardings=yes`, `ForwardAgent=no`,
  `RequestTTY=no`, `-i <observer key>`. Host/user/key/options come from a trusted
  `CollectorConfig`, never from the model. Transport is dependency-injected for
  hermetic tests.
* Admission: 1 MiB cap; hard-reject on credential material (private key blocks /
  token assignments), redact soft secret-shaped assignments; bind `run_id` +
  `source_commit`; SHA-256; write an immutable `0444` local snapshot; append an
  audit record (no secrets, no connection facts).
* `ProductionEvidenceDirectV0` (`schema_kind=experimental_noncanonical`,
  `temporary=temporary_direct_read`) — does **not** touch canonical Northstar
  `EvidenceRef`/`EvidenceSnapshot`.
* Fail-closed states: `PRODUCTION_EVIDENCE_AVAILABLE`,
  `PRODUCTION_EVIDENCE_UNAVAILABLE`, `PRODUCTION_EVIDENCE_REJECTED`,
  `PRODUCTION_EVIDENCE_IDENTITY_UNVERIFIED`. Stale evidence is never substituted;
  the worker abstains when required production evidence can't be verified.
* Engineer Worker capability `READ_PRODUCTION_DAILY_EVIDENCE` (granted to
  `DAILY_RUN_DIAGNOSTIC` only). When no collector is wired, it fails closed to
  UNAVAILABLE. The model sees only `to_model_view()` — admitted content + ids,
  never host/key/identity.

Tests: `tests/test_prod_evidence.py` (21) — selector allow/deny, admission,
hard-reject/redact secrets, size bound, identity-unverified, unavailable,
rejected-selector-never-calls-transport, fixed-argv, no-connection-fact leakage,
controller integration (available + fail-closed-unconfigured), and that
`REPAIR_CANDIDATE` cannot use the capability.

---

# OPERATOR VPS BOOTSTRAP PACKET — review, then execute manually

**Do not run any of this until you have reviewed the whole packet.** All of it
runs on the VPS as the operator (`stockbot-agent`, via sudo). **Keep a second
root/operator SSH session open the entire time** and do not close it until the
new observer account has been independently tested (Step 13). Nothing here
changes your existing admin SSH access.

### Step 1 — pre-change inventory (READ ONLY; confirms the wrapper config)
```bash
sudo ls -la /opt/stockbot/logs | tail
sudo ls -la /opt/stockbot/logs/daily_safe_$(date +%F).log
sudo ls -la /opt/stockbot/artifacts | head -40
sudo ls -la /opt/stockbot/artifacts/run_manifests 2>/dev/null | tail
sudo find /opt/stockbot -maxdepth 3 -name '*.db' -printf '%p %s\n'
# schema for the fixed queries (adjust ops/prod_evidence/stockbot-observe to match):
sudo sqlite3 -readonly /opt/stockbot/data/stockbot.db '.tables'
```
Adjust `LOG_DIR`, `MANIFEST_DIR`, `DB_PATH`, `ARTIFACT_MAP`, and the `DB_QUERY`
SQL at the top of `ops/prod_evidence/stockbot-observe` to match reality.

### Step 2 — evidence allowlist (proposed; narrow)
| capability | source (read-only) |
|---|---|
| `daily-status` | `/opt/stockbot/artifacts/daily_status.json` |
| `daily-log` | `/opt/stockbot/logs/daily_safe_<date>.log` (bounded tail) |
| `run-manifest` | `/opt/stockbot/artifacts/run_manifests/<run_id|latest>.json` |
| `artifact` | fixed map: daily_status, run_manifest, health, artifact_registry_health, decision_summary, risk_summary |
| `db-query` | fixed SELECTs: latest-daily-run, recent-health, recent-errors |

### Step 3 — explicit DENY list (never served, never granted)
`.env`, Schwab OAuth/token material, broker credentials, Claude credentials, any
`*secret*/*token*/*credential*/*.key/*.pem/id_rsa*/id_ed25519*`, SSH keys,
`/opt/stockbot/config` secrets, arbitrary paths, arbitrary SQL, whole-DB copy.
The wrapper refuses credential-shaped paths even if mis-mapped.

### Step 4 — create the dedicated observer account (no sudo, no admin groups)
```bash
sudo useradd --system --create-home --shell /usr/sbin/nologin stockbot-observer
sudo passwd -l stockbot-observer            # no password login
id stockbot-observer                         # verify: no sudo/adm/wheel groups
```
(The `nologin` shell is fine — the forced command runs the wrapper directly.)

### Step 5 — install the dedicated public key
On the **trusted local Ubuntu** box (NOT the VPS, NOT Windows), the operator
generates the observer keypair:
```bash
ssh-keygen -t ed25519 -f ~/.ssh/stockbot_observer -C stockbot-observer@trusted -N ''
chmod 600 ~/.ssh/stockbot_observer
```
Then install the **public** key on the VPS with the restricted line:
```bash
sudo install -d -o root -g root -m 0755 /home/stockbot-observer/.ssh
printf 'restrict,command="/usr/local/sbin/stockbot-observe" %s\n' \
  "$(cat ~/.ssh/stockbot_observer.pub)" | sudo tee /home/stockbot-observer/.ssh/authorized_keys
sudo chown -R stockbot-observer:stockbot-observer /home/stockbot-observer/.ssh
sudo chmod 700 /home/stockbot-observer/.ssh
sudo chmod 600 /home/stockbot-observer/.ssh/authorized_keys
```

### Step 6 — narrow read ACLs (NOT recursive read of /opt/stockbot)
Grant the observer read on *only* the allowlisted files/dirs. Prefer POSIX ACLs:
```bash
sudo setfacl -m u:stockbot-observer:--x /opt/stockbot /opt/stockbot/logs /opt/stockbot/artifacts
sudo setfacl -m u:stockbot-observer:r-- /opt/stockbot/artifacts/daily_status.json \
     /opt/stockbot/artifacts/health.json /opt/stockbot/artifacts/artifact_registry_health.json \
     /opt/stockbot/artifacts/decision_summary.json /opt/stockbot/artifacts/risk_summary.json
sudo setfacl -m u:stockbot-observer:r-x /opt/stockbot/artifacts/run_manifests
sudo setfacl -m u:stockbot-observer:r-- /opt/stockbot/data/stockbot.db      # read-only db
# daily logs: default ACL so each day's log is readable, plus existing ones:
sudo setfacl -m u:stockbot-observer:r-x /opt/stockbot/logs
sudo setfacl -d -m u:stockbot-observer:r-- /opt/stockbot/logs
sudo setfacl -m u:stockbot-observer:r-- /opt/stockbot/logs/daily_safe_*.log 2>/dev/null || true
```
Do **not** `setfacl -R` the whole tree. `.env` / config-secret dirs get **no**
grant.

### Step 7 — install the forced-command wrapper (root-owned)
Copy `ops/prod_evidence/stockbot-observe` to the VPS (via your own admin
session/scp), then:
```bash
sudo install -o root -g root -m 0755 stockbot-observe /usr/local/sbin/stockbot-observe
sudo chown root:root /usr/local/sbin/stockbot-observe   # NOT writable by observer
sudo bash -n /usr/local/sbin/stockbot-observe            # syntax check
```

### Step 8 — capability/query definitions
Already fixed inside the wrapper (Step 7). Confirm the `DB_QUERY` SQL matches the
schema from Step 1. The client can never submit SQL.

### Step 9–10 — sshd Match-User hardening + restricted key
Copy `ops/prod_evidence/sshd_stockbot_observer.conf` to
`/etc/ssh/sshd_config.d/60-stockbot-observer.conf`:
```bash
sudo install -o root -g root -m 0644 sshd_stockbot_observer.conf \
  /etc/ssh/sshd_config.d/60-stockbot-observer.conf
```
It scopes ForceCommand + `DisableForwarding`-equivalent + `PermitTTY no` to
`Match User stockbot-observer` only — your account is untouched.

### Step 11 — validate BEFORE reload
```bash
sudo sshd -t && echo SSHD_CONFIG_OK      # MUST print OK before proceeding
```
If it does not print `SSHD_CONFIG_OK`, fix the file and re-run; **do not reload.**

### Step 12 — reload with the safety session open
With your **second root session still open**:
```bash
sudo systemctl reload ssh    # (or `sshd`); reload, NOT restart
```
Confirm your existing admin session still works (open a *new* admin login in a
third terminal). Only then continue.

### Step 13 — post-change read-only tests (from the trusted local box)
```bash
KH=~/.ssh/known_hosts_stockbot_observer
ssh-keyscan -t ed25519 <VPS_IP> > "$KH"        # capture host key; verify fingerprint out-of-band
SSHO="ssh -o BatchMode=yes -o IdentitiesOnly=yes -o StrictHostKeyChecking=yes \
  -o UserKnownHostsFile=$KH -i ~/.ssh/stockbot_observer stockbot-observer@<VPS_IP>"
$SSHO daily-status
$SSHO daily-log today | tail
$SSHO run-manifest latest
$SSHO db-query latest-daily-run
```
Each returns bounded JSON/text — no secrets.

### Step 14 — negative / adversarial tests (ALL must fail)
```bash
$SSHO /bin/sh              ; echo rc=$?     # forced command -> DENIED
$SSHO "daily-log; id"      ; echo rc=$?     # metachars -> DENIED
$SSHO daily-log ../../etc/passwd            # traversal -> DENIED
$SSHO artifact ../secret                    # not allowlisted -> DENIED
$SSHO db-query "DROP TABLE runs"            # not a query id -> DENIED
$SSHO db-query recent-errors               # OK (fixed SELECT), but:
$SSHO -N -L 9999:localhost:22 ; echo rc=$?  # port-forward -> refused
$SSHO -o RequestTTY=force daily-status      # PTY -> refused
sudo -u stockbot-observer touch /opt/stockbot/artifacts/daily_status.json  # write -> Permission denied
```
Also confirm the observer cannot write any allowed file, and that
`stockbot-observer` has no sudo.

### Step 15 — ROLLBACK (if anything is wrong)
```bash
sudo rm -f /etc/ssh/sshd_config.d/60-stockbot-observer.conf
sudo sshd -t && sudo systemctl reload ssh          # restore prior sshd
sudo rm -f /usr/local/sbin/stockbot-observe
sudo userdel -r stockbot-observer                  # removes account + home + authorized_keys
# ACLs vanish with the account; optionally: sudo setfacl -R -x u:stockbot-observer /opt/stockbot
```
None of this touches your admin access, the pipeline, or production data.

---

## After bootstrap — wiring + live acceptance (separate, gated)
Once you confirm Steps 1–14 pass, wire the trusted controller's
`CollectorConfig` (`host=<VPS_IP>`, `identity_file=~/.ssh/stockbot_observer`,
`known_hosts_file=~/.ssh/known_hosts_stockbot_observer`, pinned host key) and run
a `DAILY_RUN_DIAGNOSTIC` with `READ_PRODUCTION_DAILY_EVIDENCE`.

If the harness still blocks the collector's SSH call, run the **single restricted
invocation** yourself and hand the output back for admission — e.g.:
```bash
ssh -o BatchMode=yes -o IdentitiesOnly=yes -o StrictHostKeyChecking=yes \
  -o UserKnownHostsFile=~/.ssh/known_hosts_stockbot_observer \
  -i ~/.ssh/stockbot_observer stockbot-observer@<VPS_IP> daily-status
```
— rather than broadening Claude's SSH authority. Do **not** add a broad
`Bash(ssh *)` rule; any future automation is scoped to this single observer
capability.

---

# Permanent controller key — enrollment + host-key pin + smoke (operator steps)

The trusted controller's permanent dedicated key was generated on the trusted
Ubuntu host (this mission):

- private key: `~/.ssh/stockbot_engineer` (mode 0600, owner = operator; `~/.ssh` 0700)
- public fingerprint: `SHA256:BPNnnxrpQd147uOv45eHGl/UbC65lBncxCwTwuoR3Lw` (ED25519)
- comment: `stockbot-engineer-trusted-controller`

Isolation proven: the private key exists ONLY on the trusted Ubuntu host, outside
the repo, untracked by git, not in any repo file; the StockBot-Agent-Lab distro
has no `/home/pesan`, no copy of the key anywhere, and `rd-worker` cannot read the
path (it does not exist in that distro). It is never in the sandbox, model input,
`.env`, telemetry, or `ProductionEvidenceDirectV0`.

## Gate A — fresh external operator SSH (HUMAN)
Before enrolling the permanent key, open a NEW SSH session from the machine you
normally use to administer the VPS and confirm the *admin* login still works. Do
not substitute a localhost/self-login. Record: timestamp, host identity, success
(no secrets). If it fails, STOP — restore operator access first.

## Phase E/F — enroll ONLY the public key (certified restricted template)
Append this single line to `stockbot-engineer`'s `authorized_keys` on the VPS
(root-controlled file, not writable by `stockbot-engineer`). It preserves the
certified restrictions (`restrict` implies no-agent/port/x11-forwarding, no-pty,
no-user-rc) and pins the forced command:

```
restrict,command="/usr/local/sbin/stockbot-engineer-read" ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAILl7UC9XUX33ZVxeR9+y6K36xggxUx+hZE9FrtsbhDUh stockbot-engineer-trusted-controller
```

Apply via your existing VPS root/Claude session (the local harness blocks
outbound SSH; do not route around it). The change must be: one restricted key
entry, no sshd change, no permission broadening, no extra account change. Then
verify from the VPS: the fingerprint matches `SHA256:BPNnnxrpQd147uOv45eHGl/UbC65lBncxCwTwuoR3Lw`,
the `restrict,command=…` options are present, there is no duplicate unrestricted
key, and no private key exists on the VPS.

## Phase H — pin the VPS host identity (dedicated known_hosts)
Do NOT use `StrictHostKeyChecking=no`. Capture the host key into a dedicated
known_hosts and verify the fingerprint out-of-band (against the VPS-reported
value) BEFORE trusting it:
```bash
ssh-keyscan -t ed25519 -p 22 46.224.25.135 > ~/.ssh/known_hosts_stockbot_engineer
ssh-keygen -lf ~/.ssh/known_hosts_stockbot_engineer   # compare to the VPS-reported host fingerprint
```
Record: host `46.224.25.135`, key type ed25519, the fingerprint, and the
verification source. The collector uses `StrictHostKeyChecking=yes` +
`UserKnownHostsFile=~/.ssh/known_hosts_stockbot_engineer`; a changed key →
`PRODUCTION_EVIDENCE_UNAVAILABLE` (never auto-accept).

## Phase G — permanent-key smoke (positive + negative)
```bash
SSHE="ssh -o BatchMode=yes -o IdentitiesOnly=yes -o StrictHostKeyChecking=yes \
  -o UserKnownHostsFile=$HOME/.ssh/known_hosts_stockbot_engineer \
  -i $HOME/.ssh/stockbot_engineer stockbot-engineer@46.224.25.135"
$SSHE daily-status        ; echo rc=$?     # POSITIVE: bounded evidence, rc=0
$SSHE run-manifest        ; echo rc=$?     # POSITIVE
$SSHE id                  ; echo rc=$?     # NEGATIVE: forced command -> denied
$SSHE /bin/sh             ; echo rc=$?     # NEGATIVE
$SSHE -N -L 9999:localhost:22 ; echo rc=$? # NEGATIVE: forwarding refused
```
Purpose: prove the PERMANENT key is bound to the same restricted authority.

## Phase W — trusted collector smoke (transport/admission only, NOT model diagnosis)
The local harness blocks the collector's outbound SSH, so run the single fixed
invocation yourself and hand the output back for admission through the same
pipeline (size bound → secret screen → run-identity → hash → immutable snapshot):
```bash
$SSHE daily-status       # capture stdout; paste it back for local admission
$SSHE run-manifest
```
This certifies transport + admission separately from model behavior. Do NOT ask
the Engineer model to diagnose production until that end-to-end gate is
explicitly authorized.

## Model-visible capability enum (the ONLY names the worker may request)
`PROD_DAILY_STATUS`, `PROD_DAILY_STATUS_JSON`, `PROD_PIPELINE_STATUS`,
`PROD_RUN_MANIFEST`, `PROD_HEALTH`, `PROD_LAST_SUCCESS`, `PROD_DAILY_LOG`
(date selector), `PROD_DAILY_CHECK` (date selector), `PROD_DB_LATEST_DAILY_RUN`,
`PROD_DB_RECENT_HEALTH`, `PROD_DB_RECENT_ERRORS`. These map deterministically to
the certified server verbs; the model never names a verb, path, or SQL.

## Run-identity, secrets, snapshot (local admission policy)
- **Run identity:** an admitted piece's `run_id`/`source_commit` is bound; a
  mismatch against an expected run → `IDENTITY_UNVERIFIED` (abstain). Capabilities
  without a `run_id` by design (e.g. status) carry freshness via
  `generated_at`/`retrieved_at`. `assert_coherent()` refuses mixed-run evidence —
  yesterday's run / prior artifact / mixed-run is never silently substituted.
- **Secrets:** FAIL CLOSED — any credential-shaped content (private-key blocks,
  `token/api_key/password/...=<value>`, AWS ids, bearer/authorization) →
  `REJECTED`. Already-`<redacted>` values pass. Secret values are never logged.
- **Snapshot:** immutable `0444`, atomic create, **no overwrite** of an existing
  retrieval id, symlink-safe dir + target checks; outside worker-writable state.
  The worker receives only the admitted local snapshot, never live SSH stdout.

## Future replacement + inheritance
`DIRECT PRODUCTION EVIDENCE V0 IS TEMPORARY`, replaced by governed Agent Export /
authenticated evidence admission. Future Finance/Quant/Design workers do **not**
inherit this VPS read capability; each additional consumer needs explicit
authority review.